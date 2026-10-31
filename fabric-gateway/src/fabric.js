'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const grpc = require('@grpc/grpc-js');
const { connect, signers } = require('@hyperledger/fabric-gateway');

const CHANNEL = process.env.FABRIC_CHANNEL || 'pdschannel';
const CHAINCODE = process.env.FABRIC_CHAINCODE || 'pds';
const WALLET = path.resolve(process.env.WALLET_DIR || path.join(__dirname, '..', 'wallet'));

// test-network defaults: Org1 peer on 7051, Org2 peer on 9051
const PEERS = {
    Org1MSP: {
        endpoint: process.env.ORG1_PEER_ENDPOINT || 'localhost:7051',
        hostAlias: 'peer0.org1.example.com',
        tlsCert: path.join(WALLET, '_tls', 'org1-tlsca.pem'),
    },
    Org2MSP: {
        endpoint: process.env.ORG2_PEER_ENDPOINT || 'localhost:9051',
        hostAlias: 'peer0.org2.example.com',
        tlsCert: path.join(WALLET, '_tls', 'org2-tlsca.pem'),
    },
};

// Only these chaincode functions can be reached through the HTTP API.
const SUBMIT_FUNCTIONS = new Set([
    'RegisterBag', 'DispatchBag', 'ReceiveBag', 'RecordReconciliation',
    'RecordCorrection', 'GrantRole', 'RevokeRole',
]);
const EVALUATE_FUNCTIONS = new Set(['GetBagStatus', 'GetTransactionHistory', 'GetRole']);

const clients = new Map();

function loadIdentity(name) {
    if (!/^[A-Za-z0-9._-]{3,64}$/.test(name || '')) throw httpError(400, 'invalid identity name');
    const dir = path.join(WALLET, name);
    const meta = path.join(dir, 'identity.json');
    if (!fs.existsSync(meta)) throw httpError(404, `unknown Fabric identity '${name}'`);
    const { mspId } = JSON.parse(fs.readFileSync(meta, 'utf8'));
    const credentials = fs.readFileSync(path.join(dir, 'cert.pem'));
    const privateKey = crypto.createPrivateKey(fs.readFileSync(path.join(dir, 'key.pem')));
    return { mspId, credentials, signer: signers.newPrivateKeySigner(privateKey) };
}

function grpcClient(mspId) {
    if (clients.has(mspId)) return clients.get(mspId);
    const p = PEERS[mspId];
    if (!p) throw httpError(400, `no peer configured for ${mspId}`);
    const creds = grpc.credentials.createSsl(fs.readFileSync(p.tlsCert));
    const client = new grpc.Client(p.endpoint, creds, { 'grpc.ssl_target_name_override': p.hostAlias });
    clients.set(mspId, client);
    return client;
}

function withGateway(identityName, fn) {
    const id = loadIdentity(identityName);
    const gateway = connect({
        client: grpcClient(id.mspId),
        identity: { mspId: id.mspId, credentials: id.credentials },
        signer: id.signer,
        evaluateOptions: () => ({ deadline: Date.now() + 10_000 }),
        endorseOptions: () => ({ deadline: Date.now() + 20_000 }),
        submitOptions: () => ({ deadline: Date.now() + 20_000 }),
        commitStatusOptions: () => ({ deadline: Date.now() + 60_000 }),
    });
    const contract = gateway.getNetwork(CHANNEL).getContract(CHAINCODE);
    return Promise.resolve(fn(contract)).finally(() => gateway.close());
}

/** Non-secret metadata about a wallet identity (never returns key material). */
function identityInfo(name) {
    if (!/^[A-Za-z0-9._-]{3,64}$/.test(name || '')) return { exists: false };
    const meta = path.join(WALLET, name, 'identity.json');
    if (!fs.existsSync(meta)) return { exists: false };
    const { mspId, role, location } = JSON.parse(fs.readFileSync(meta, 'utf8'));
    return { exists: true, mspId, role, location };
}

function httpError(status, message) {
    const e = new Error(message);
    e.status = status;
    return e;
}

/** Extract the chaincode's own error message from a Gateway error. */
function describeError(err) {
    const details = err && err.details;
    if (Array.isArray(details) && details.length) {
        return details.map((d) => d.message).join(' | ');
    }
    return (err && err.message) || String(err);
}

async function submit(identity, fn, args) {
    if (!SUBMIT_FUNCTIONS.has(fn)) throw httpError(400, `function '${fn}' is not allowed`);
    return withGateway(identity, async (contract) => {
        const proposal = contract.newProposal(fn, { arguments: args.map(String) });
        const txId = proposal.getTransactionId();
        const transaction = await proposal.endorse();
        const result = Buffer.from(transaction.getResult()).toString('utf8');
        const commit = await transaction.submit();
        const status = await commit.getStatus();
        if (!status.successful) {
            throw httpError(409, `transaction ${txId} failed to commit with code ${status.code}`);
        }
        return { txId, blockNumber: String(status.blockNumber), result: result ? JSON.parse(result) : null };
    });
}

async function evaluate(identity, fn, args) {
    if (!EVALUATE_FUNCTIONS.has(fn)) throw httpError(400, `function '${fn}' is not allowed`);
    return withGateway(identity, async (contract) => {
        const bytes = await contract.evaluateTransaction(fn, ...args.map(String));
        const text = Buffer.from(bytes).toString('utf8');
        return { result: text ? JSON.parse(text) : null };
    });
}

module.exports = { submit, evaluate, identityInfo, describeError, httpError, CHANNEL, CHAINCODE, WALLET };

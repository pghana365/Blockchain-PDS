'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

// fabric-contract-api is needed by the contract class
const PdsContract = require('../lib/pds-contract');

// ---------------------------------------------------------------- test doubles
class FakeLedger {
    constructor() { this.state = new Map(); this.history = new Map(); this.n = 0; }
}

function makeCtx(ledger, { msp, name, role, location }) {
    const txId = `tx${String(++ledger.n).padStart(6, '0')}`;
    const pending = new Map();
    const stub = {
        getTxID: () => txId,
        getTxTimestamp: () => ({ seconds: { toString: () => String(1760000000 + ledger.n) }, nanos: 0 }),
        getState: async (k) => Buffer.from(pending.get(k) ?? ledger.state.get(k) ?? ''),
        putState: async (k, v) => { pending.set(k, Buffer.from(v).toString()); },
        setEvent: () => {},
        getHistoryForKey: async (k) => {
            const items = (ledger.history.get(k) || []).slice();
            let i = 0;
            return {
                next: async () => (i < items.length ? { value: items[i++], done: i >= items.length } : { done: true }),
                close: async () => {},
            };
        },
    };
    const clientIdentity = {
        getID: () => `x509::CN=${name},OU=client,O=Hyperledger::CN=ca.example.com`,
        getMSPID: () => msp,
        getAttributeValue: (a) => ({ 'pds.role': role, 'pds.location': location })[a] || null,
    };
    const ctx = { stub, clientIdentity };
    ctx.commit = () => {
        for (const [k, v] of pending) {
            ledger.state.set(k, v);
            if (k.startsWith('BAG_')) {
                const h = ledger.history.get(k) || [];
                h.push({ txId, timestamp: { seconds: 1760000000 + ledger.n }, isDelete: false, value: Buffer.from(v) });
                ledger.history.set(k, h);
            }
        }
    };
    return ctx;
}

const IDS = {
    admin: { msp: 'Org1MSP', name: 'pdsadmin', role: 'admin', location: '' },
    mls1: { msp: 'Org1MSP', name: 'mls01op', role: 'mls', location: 'MLS-01' },
    mls2: { msp: 'Org1MSP', name: 'mls02op', role: 'mls', location: 'MLS-02' },
    fps1: { msp: 'Org2MSP', name: 'fps01op', role: 'fps', location: 'FPS-01' },
    fps2: { msp: 'Org2MSP', name: 'fps02op', role: 'fps', location: 'FPS-02' },
    auditor: { msp: 'Org2MSP', name: 'auditor1', role: 'auditor', location: '' },
    rogue: { msp: 'Org2MSP', name: 'rogue', role: '', location: '' },
};

let reqN = 0;
const rid = () => `req-${String(++reqN).padStart(8, '0')}`;
const commit = (w, salt) => crypto.createHash('sha256').update(`BAG-0001|${w}|${salt}`).digest('hex');

const cc = new PdsContract();

async function invoke(ledger, who, fn, ...args) {
    const ctx = makeCtx(ledger, IDS[who]);
    const res = await cc[fn](ctx, ...args);
    ctx.commit();
    return res && res.length ? JSON.parse(res) : res;
}

async function setup() {
    const l = new FakeLedger();
    await invoke(l, 'admin', 'RegisterBag', 'BAG-0001', 'Rice', 'MLS-01', 'FPS-01', 'CONS-001', rid());
    return l;
}

// --------------------------------------------------------------------- tests
test('1. authorised MLS operator dispatches a bag; ledger holds only the commitment', async () => {
    const l = await setup();
    const c = commit('50.500', 'salt1');
    const bag = await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', c, rid());
    assert.equal(bag.status, 'IN_TRANSIT');
    assert.equal(bag.dispatchCommitment, c);
    assert.ok(!JSON.stringify(bag).includes('50.5'), 'weight must not appear on ledger');
});

test('2. unauthorised identities cannot dispatch', async () => {
    const l = await setup();
    const c = commit('50.500', 's');
    for (const who of ['fps1', 'auditor', 'rogue', 'mls2', 'admin']) {
        await assert.rejects(invoke(l, who, 'DispatchBag', 'BAG-0001', c, rid()), /ACCESS_DENIED/, who);
    }
});

test('3. FPS cannot read bag before receipt without seeing any weight', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50.500', 'x'), rid());
    const view = JSON.stringify(await invoke(l, 'fps1', 'GetBagStatus', 'BAG-0001'));
    assert.ok(!view.includes('50.5'));
    assert.ok(!/weight/i.test(view));
});

test('4. FPS records receipt after dispatch', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50.500', 'a'), rid());
    const bag = await invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('47.500', 'b'), rid());
    assert.equal(bag.status, 'RECEIVED');
});

test('5/6. matching and within-tolerance differences give MATCHED', async () => {
    for (const diff of ['0', '0.15', '0.2']) {
        const l = await setup();
        await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid());
        await invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('50', 'b'), rid());
        const bag = await invoke(l, 'fps1', 'RecordReconciliation', 'BAG-0001', 'MATCHED', diff, '0.2', rid());
        assert.equal(bag.status, 'MATCHED');
    }
});

test('7. difference beyond tolerance is FLAGGED, and a wrong claim is refused', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50.5', 'a'), rid());
    await invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('47.5', 'b'), rid());
    await assert.rejects(
        invoke(l, 'fps1', 'RecordReconciliation', 'BAG-0001', 'MATCHED', '3.0', '0.2', rid()),
        /INCONSISTENT_RESULT/);
    const bag = await invoke(l, 'fps1', 'RecordReconciliation', 'BAG-0001', 'FLAGGED', '3.0', '0.2', rid());
    assert.equal(bag.status, 'FLAGGED');
    assert.match(bag.reconciliation.note, /further verification/);
    assert.match(bag.reconciliation.note, /Not evidence of theft/);
});

test('8. FPS cannot receive a bag assigned to another FPS', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid());
    await assert.rejects(invoke(l, 'fps2', 'ReceiveBag', 'BAG-0001', commit('50', 'b'), rid()), /ACCESS_DENIED/);
    await assert.rejects(invoke(l, 'fps2', 'GetBagStatus', 'BAG-0001'), /ACCESS_DENIED/);
});

test('9. duplicate dispatch is rejected (state and request replay)', async () => {
    const l = await setup();
    const r = rid();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), r);
    await assert.rejects(invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('51', 'a'), rid()), /INVALID_STATE/);
    await assert.rejects(invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), r), /INVALID_STATE|DUPLICATE_REQUEST/);
});

test('10. duplicate receipt is rejected; receipt before dispatch is rejected', async () => {
    const l = await setup();
    await assert.rejects(invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('50', 'b'), rid()), /INVALID_STATE/);
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid());
    await invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('50', 'b'), rid());
    await assert.rejects(invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('49', 'b'), rid()), /INVALID_STATE/);
});

test('11. nobody can modify historical records; admin corrections are separate events', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid());
    for (const who of ['mls1', 'fps1', 'auditor']) {
        await assert.rejects(invoke(l, who, 'RecordCorrection', 'BAG-0001', 'attempted edit', rid()), /ACCESS_DENIED/);
        await assert.rejects(invoke(l, who, 'RegisterBag', 'BAG-0009', 'Rice', 'MLS-01', 'FPS-01', 'C', rid()), /ACCESS_DENIED/);
    }
    const before = l.state.get('BAG_BAG-0001');
    const corr = await invoke(l, 'admin', 'RecordCorrection', 'BAG-0001', 'Operator typo reported by MLS-01', rid());
    assert.equal(corr.docType, 'correction');
    assert.equal(l.state.get('BAG_BAG-0001'), before, 'bag record untouched by correction');
    await assert.rejects(invoke(l, 'admin', 'RegisterBag', 'BAG-0001', 'Rice', 'MLS-01', 'FPS-01', 'C', rid()), /DUPLICATE_BAG/);
});

test('12. auditor can read full history of a bag', async () => {
    const l = await setup();
    await invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50.5', 'a'), rid());
    await invoke(l, 'fps1', 'ReceiveBag', 'BAG-0001', commit('47.5', 'b'), rid());
    await invoke(l, 'fps1', 'RecordReconciliation', 'BAG-0001', 'FLAGGED', '3.0', '0.2', rid());
    const hist = await invoke(l, 'auditor', 'GetTransactionHistory', 'BAG-0001');
    assert.deepEqual(hist.map((h) => h.value.status), ['REGISTERED', 'IN_TRANSIT', 'RECEIVED', 'FLAGGED']);
    assert.ok(hist.every((h) => h.txId));
});

test('revoked identity is refused; role grant is admin only', async () => {
    const l = await setup();
    await assert.rejects(invoke(l, 'mls1', 'GrantRole', 'mls01op', 'mls', 'MLS-01', rid()), /ACCESS_DENIED/);
    await invoke(l, 'admin', 'GrantRole', 'mls01op', 'mls', 'MLS-01', rid());
    await invoke(l, 'admin', 'RevokeRole', 'mls01op', rid());
    await assert.rejects(invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid()), /revoked/);
});

test('input validation: bad bag id, bad commitment, wrong-org role', async () => {
    const l = await setup();
    await assert.rejects(invoke(l, 'mls1', 'DispatchBag', 'bag1', commit('50', 'a'), rid()), /INVALID_INPUT/);
    await assert.rejects(invoke(l, 'mls1', 'DispatchBag', 'BAG-0001', 'abc', rid()), /INVALID_INPUT/);
    IDS.fakeMls = { msp: 'Org2MSP', name: 'x', role: 'mls', location: 'MLS-01' };
    await assert.rejects(invoke(l, 'fakeMls', 'DispatchBag', 'BAG-0001', commit('50', 'a'), rid()), /not valid for organisation/);
});

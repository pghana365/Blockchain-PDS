'use strict';

/**
 * PDS Blind Weight Verification - chaincode.
 *
 * WHAT IS ON-CHAIN: bag metadata, lifecycle state, SHA-256 weight COMMITMENTS,
 * reconciliation result (MATCHED/FLAGGED + difference + tolerance), audit
 * events, role grants. Exact weights and salts are NEVER sent to the ledger.
 *
 * Access control uses:
 *   - MSP membership (Org1MSP = MLS / admin side, Org2MSP = FPS side)
 *   - certificate attributes issued by Fabric CA:
 *        pds.role      = admin | mls | fps | auditor
 *        pds.location  = e.g. MLS-01 / FPS-01   (empty for admin/auditor)
 *   - an on-ledger revocation list managed with GrantRole / RevokeRole
 */

const { Contract } = require('fabric-contract-api');

const MLS_MSP = 'Org1MSP';
const FPS_MSP = 'Org2MSP';

const ROLES = ['admin', 'mls', 'fps', 'auditor'];
const ROLE_MSP = { admin: MLS_MSP, mls: MLS_MSP, fps: FPS_MSP };

const STATUS = {
    REGISTERED: 'REGISTERED',
    IN_TRANSIT: 'IN_TRANSIT',
    RECEIVED: 'RECEIVED',
    MATCHED: 'MATCHED',
    FLAGGED: 'FLAGGED',
};

const BAG_RE = /^BAG-\d{4,}$/;
const LOC_RE = /^(MLS|FPS)-[A-Z0-9]{2,10}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;
const REQ_RE = /^[A-Za-z0-9_-]{8,64}$/;
const NUM_RE = /^\d+(\.\d{1,3})?$/;

const bagKey = (id) => `BAG_${id}`;
const reqKey = (id) => `REQ_${id}`;
const roleKey = (name) => `ROLE_${name}`;

class PdsContract extends Contract {
    constructor() {
        super('PdsContract');
    }

    // ------------------------------------------------------------------ helpers
    _caller(ctx) {
        const id = ctx.clientIdentity;
        const m = /CN=([^,:/]+)/.exec(id.getID());
        return {
            msp: id.getMSPID(),
            name: m ? m[1] : 'unknown',
            role: id.getAttributeValue('pds.role') || '',
            location: id.getAttributeValue('pds.location') || '',
        };
    }

    async _authorize(ctx, allowedRoles) {
        const c = this._caller(ctx);
        if (!ROLES.includes(c.role)) {
            throw new Error('ACCESS_DENIED: identity has no valid pds.role attribute');
        }
        if (!allowedRoles.includes(c.role)) {
            throw new Error(`ACCESS_DENIED: role '${c.role}' may not perform this operation`);
        }
        if (ROLE_MSP[c.role] && ROLE_MSP[c.role] !== c.msp) {
            throw new Error(`ACCESS_DENIED: role '${c.role}' not valid for organisation ${c.msp}`);
        }
        // on-ledger revocation list
        const raw = await ctx.stub.getState(roleKey(c.name));
        if (raw && raw.length) {
            const grant = JSON.parse(raw.toString());
            if (!grant.active) {
                throw new Error(`ACCESS_DENIED: identity '${c.name}' has been revoked`);
            }
        }
        return c;
    }

    _txTime(ctx) {
        const ts = ctx.stub.getTxTimestamp();
        const seconds = Number(ts.seconds.toString());
        return new Date(seconds * 1000 + Math.floor((ts.nanos || 0) / 1e6)).toISOString();
    }

    async _guardRequest(ctx, requestId) {
        if (!REQ_RE.test(requestId || '')) {
            throw new Error('INVALID_INPUT: requestId must be 8-64 chars [A-Za-z0-9_-]');
        }
        const existing = await ctx.stub.getState(reqKey(requestId));
        if (existing && existing.length) {
            throw new Error(`DUPLICATE_REQUEST: requestId ${requestId} already processed`);
        }
        await ctx.stub.putState(reqKey(requestId), Buffer.from(ctx.stub.getTxID()));
    }

    _checkBagId(bagId) {
        if (!BAG_RE.test(bagId || '')) throw new Error('INVALID_INPUT: bagId must look like BAG-0001');
    }

    _checkCommitment(c) {
        if (!HEX64_RE.test(c || '')) throw new Error('INVALID_INPUT: commitment must be 64 lowercase hex chars (SHA-256)');
    }

    async _getBag(ctx, bagId) {
        this._checkBagId(bagId);
        const raw = await ctx.stub.getState(bagKey(bagId));
        if (!raw || !raw.length) throw new Error(`BAG_NOT_FOUND: ${bagId}`);
        return JSON.parse(raw.toString());
    }

    async _putBag(ctx, bag) {
        await ctx.stub.putState(bagKey(bag.bagId), Buffer.from(JSON.stringify(bag)));
    }

    _event(ctx, name, payload) {
        ctx.stub.setEvent(name, Buffer.from(JSON.stringify(payload)));
    }

    /** read access: admin/auditor everything, mls/fps only their own bags */
    _canRead(c, bag) {
        if (c.role === 'admin' || c.role === 'auditor') return true;
        if (c.role === 'mls') return c.location === bag.originMls;
        if (c.role === 'fps') return c.location === bag.destinationFps;
        return false;
    }

    // ------------------------------------------------------------ transactions
    async RegisterBag(ctx, bagId, commodity, originMls, destinationFps, consignmentId, requestId) {
        const c = await this._authorize(ctx, ['admin']);
        this._checkBagId(bagId);
        if (!LOC_RE.test(originMls) || !originMls.startsWith('MLS-')) throw new Error('INVALID_INPUT: originMls');
        if (!LOC_RE.test(destinationFps) || !destinationFps.startsWith('FPS-')) throw new Error('INVALID_INPUT: destinationFps');
        if (!commodity || commodity.length > 50) throw new Error('INVALID_INPUT: commodity');
        if (!consignmentId || consignmentId.length > 40) throw new Error('INVALID_INPUT: consignmentId');
        await this._guardRequest(ctx, requestId);

        const existing = await ctx.stub.getState(bagKey(bagId));
        if (existing && existing.length) throw new Error(`DUPLICATE_BAG: ${bagId} already registered`);

        const bag = {
            docType: 'bag',
            bagId,
            commodity,
            originMls,
            destinationFps,
            consignmentId,
            status: STATUS.REGISTERED,
            registeredBy: c.name,
            registeredAt: this._txTime(ctx),
            registerTxId: ctx.stub.getTxID(),
        };
        await this._putBag(ctx, bag);
        this._event(ctx, 'BagRegistered', { bagId, txId: bag.registerTxId });
        return JSON.stringify(bag);
    }

    async DispatchBag(ctx, bagId, dispatchCommitment, requestId) {
        const c = await this._authorize(ctx, ['mls']);
        this._checkCommitment(dispatchCommitment);
        const bag = await this._getBag(ctx, bagId);
        if (c.location !== bag.originMls) {
            throw new Error(`ACCESS_DENIED: bag originates at ${bag.originMls}, caller is at ${c.location}`);
        }
        if (bag.status !== STATUS.REGISTERED) {
            throw new Error(`INVALID_STATE: bag is ${bag.status}; dispatch requires REGISTERED`);
        }
        await this._guardRequest(ctx, requestId);

        bag.status = STATUS.IN_TRANSIT;
        bag.dispatchCommitment = dispatchCommitment;
        bag.dispatchedBy = c.name;
        bag.dispatchedAt = this._txTime(ctx);
        bag.dispatchTxId = ctx.stub.getTxID();
        await this._putBag(ctx, bag);
        this._event(ctx, 'BagDispatched', { bagId, txId: bag.dispatchTxId });
        return JSON.stringify(bag);
    }

    async ReceiveBag(ctx, bagId, receiptCommitment, requestId) {
        const c = await this._authorize(ctx, ['fps']);
        this._checkCommitment(receiptCommitment);
        const bag = await this._getBag(ctx, bagId);
        if (c.location !== bag.destinationFps) {
            throw new Error(`ACCESS_DENIED: bag is destined for ${bag.destinationFps}, caller is at ${c.location}`);
        }
        if (bag.status !== STATUS.IN_TRANSIT) {
            throw new Error(`INVALID_STATE: bag is ${bag.status}; receipt requires IN_TRANSIT`);
        }
        await this._guardRequest(ctx, requestId);

        bag.status = STATUS.RECEIVED;
        bag.receiptCommitment = receiptCommitment;
        bag.receivedBy = c.name;
        bag.receivedAt = this._txTime(ctx);
        bag.receiptTxId = ctx.stub.getTxID();
        await this._putBag(ctx, bag);
        this._event(ctx, 'BagReceived', { bagId, txId: bag.receiptTxId });
        return JSON.stringify(bag);
    }

    /**
     * Records the reconciliation outcome. The comparison is computed by the
     * backend (which alone holds both exact weights); the ledger stores the
     * outcome and the commitments allow later verification of the inputs.
     */
    async RecordReconciliation(ctx, bagId, result, differenceKg, toleranceKg, requestId) {
        const c = await this._authorize(ctx, ['fps', 'admin']);
        if (result !== STATUS.MATCHED && result !== STATUS.FLAGGED) {
            throw new Error('INVALID_INPUT: result must be MATCHED or FLAGGED');
        }
        if (!NUM_RE.test(differenceKg) || !NUM_RE.test(toleranceKg)) {
            throw new Error('INVALID_INPUT: differenceKg/toleranceKg must be non-negative decimals');
        }
        const bag = await this._getBag(ctx, bagId);
        if (c.role === 'fps' && c.location !== bag.destinationFps) {
            throw new Error('ACCESS_DENIED: not the destination FPS');
        }
        if (bag.status !== STATUS.RECEIVED) {
            throw new Error(`INVALID_STATE: bag is ${bag.status}; reconciliation requires RECEIVED`);
        }
        const expected = Number(differenceKg) <= Number(toleranceKg) ? STATUS.MATCHED : STATUS.FLAGGED;
        if (expected !== result) {
            throw new Error(`INCONSISTENT_RESULT: difference ${differenceKg} vs tolerance ${toleranceKg} implies ${expected}`);
        }
        await this._guardRequest(ctx, requestId);

        bag.status = result;
        bag.reconciliation = {
            result,
            differenceKg,
            toleranceKg,
            recordedBy: c.name,
            recordedAt: this._txTime(ctx),
            txId: ctx.stub.getTxID(),
            note: result === STATUS.FLAGGED
                ? 'Discrepancy flagged - requires further verification. Not evidence of theft or fraud.'
                : 'Within tolerance.',
        };
        await this._putBag(ctx, bag);
        this._event(ctx, 'BagReconciled', { bagId, result, txId: bag.reconciliation.txId });
        return JSON.stringify(bag);
    }

    /** Admin corrections never overwrite history: they are separate audit events. */
    async RecordCorrection(ctx, bagId, note, requestId) {
        const c = await this._authorize(ctx, ['admin']);
        if (!note || note.length < 5 || note.length > 500) throw new Error('INVALID_INPUT: note must be 5-500 chars');
        await this._getBag(ctx, bagId);
        await this._guardRequest(ctx, requestId);
        const entry = {
            docType: 'correction',
            bagId,
            note,
            by: c.name,
            at: this._txTime(ctx),
            txId: ctx.stub.getTxID(),
        };
        await ctx.stub.putState(`CORR_${bagId}_${entry.txId}`, Buffer.from(JSON.stringify(entry)));
        this._event(ctx, 'CorrectionRecorded', { bagId, txId: entry.txId });
        return JSON.stringify(entry);
    }

    async GrantRole(ctx, enrollmentId, role, location, requestId) {
        const c = await this._authorize(ctx, ['admin']);
        if (!/^[A-Za-z0-9._-]{3,64}$/.test(enrollmentId || '')) throw new Error('INVALID_INPUT: enrollmentId');
        if (!ROLES.includes(role)) throw new Error('INVALID_INPUT: role');
        if ((role === 'mls' || role === 'fps') && !LOC_RE.test(location || '')) throw new Error('INVALID_INPUT: location');
        await this._guardRequest(ctx, requestId);
        const grant = {
            docType: 'roleGrant',
            enrollmentId,
            role,
            location: location || '',
            active: true,
            grantedBy: c.name,
            at: this._txTime(ctx),
            txId: ctx.stub.getTxID(),
        };
        await ctx.stub.putState(roleKey(enrollmentId), Buffer.from(JSON.stringify(grant)));
        this._event(ctx, 'RoleGranted', { enrollmentId, role });
        return JSON.stringify(grant);
    }

    async RevokeRole(ctx, enrollmentId, requestId) {
        const c = await this._authorize(ctx, ['admin']);
        await this._guardRequest(ctx, requestId);
        const raw = await ctx.stub.getState(roleKey(enrollmentId));
        const prev = raw && raw.length ? JSON.parse(raw.toString()) : { enrollmentId, role: '', location: '' };
        const grant = {
            ...prev,
            docType: 'roleGrant',
            active: false,
            revokedBy: c.name,
            at: this._txTime(ctx),
            txId: ctx.stub.getTxID(),
        };
        await ctx.stub.putState(roleKey(enrollmentId), Buffer.from(JSON.stringify(grant)));
        this._event(ctx, 'RoleRevoked', { enrollmentId });
        return JSON.stringify(grant);
    }

    // ------------------------------------------------------------------ queries
    async GetBagStatus(ctx, bagId) {
        const c = await this._authorize(ctx, ['admin', 'mls', 'fps', 'auditor']);
        const bag = await this._getBag(ctx, bagId);
        if (!this._canRead(c, bag)) throw new Error('ACCESS_DENIED: bag is not associated with your location');
        return JSON.stringify(bag);
    }

    async GetTransactionHistory(ctx, bagId) {
        const c = await this._authorize(ctx, ['admin', 'mls', 'fps', 'auditor']);
        const bag = await this._getBag(ctx, bagId);
        if (!this._canRead(c, bag)) throw new Error('ACCESS_DENIED: bag is not associated with your location');

        const out = [];
        const it = await ctx.stub.getHistoryForKey(bagKey(bagId));
        try {
            for (;;) {
                const res = await it.next();
                if (res.value) {
                    const ts = res.value.timestamp;
                    const secs = ts && ts.seconds ? Number(ts.seconds.toString()) : 0;
                    out.push({
                        txId: res.value.txId,
                        timestamp: new Date(secs * 1000).toISOString(),
                        isDelete: !!res.value.isDelete,
                        value: res.value.value && res.value.value.length
                            ? JSON.parse(Buffer.from(res.value.value).toString())
                            : null,
                    });
                }
                if (res.done) break;
            }
        } finally {
            await it.close();
        }
        return JSON.stringify(out);
    }

    async GetRole(ctx, enrollmentId) {
        await this._authorize(ctx, ['admin', 'auditor']);
        const raw = await ctx.stub.getState(roleKey(enrollmentId));
        if (!raw || !raw.length) throw new Error(`ROLE_NOT_FOUND: ${enrollmentId}`);
        return raw.toString();
    }
}

module.exports = PdsContract;
module.exports.STATUS = STATUS;

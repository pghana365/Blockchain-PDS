'use strict';
/**
 * Live end-to-end test of the chaincode through the Gateway SDK.
 * Requires the Fabric network + chaincode to be running.
 *   node src/smoke-test.js
 */
const crypto = require('node:crypto');
const fabric = require('./fabric');

const sha = (bagId, w, salt) => crypto.createHash('sha256').update(`${bagId}|${w}|${salt}`).digest('hex');
const rid = () => crypto.randomBytes(12).toString('hex');
const bagId = `BAG-${String(Math.floor(Math.random() * 9000) + 1000)}`;

async function expectFail(label, p, re) {
    try { await p; } catch (e) {
        const msg = fabric.describeError(e);
        if (re.test(msg)) { console.log(`  PASS  ${label} -> rejected (${msg.slice(0, 90)})`); return; }
        throw new Error(`${label}: wrong error: ${msg}`);
    }
    throw new Error(`${label}: expected failure but succeeded`);
}

(async () => {
    console.log('Bag under test:', bagId);
    let r = await fabric.submit('pdsadmin', 'RegisterBag', [bagId, 'Rice', 'MLS-01', 'FPS-01', 'CONS-SMOKE', rid()]);
    console.log('  PASS  RegisterBag tx', r.txId.slice(0, 16), 'block', r.blockNumber);

    await expectFail('wrong MLS cannot dispatch', fabric.submit('mls02op', 'DispatchBag', [bagId, sha(bagId, '50.5', 'x'), rid()]), /ACCESS_DENIED/);
    await expectFail('FPS cannot dispatch', fabric.submit('fps01op', 'DispatchBag', [bagId, sha(bagId, '50.5', 'x'), rid()]), /ACCESS_DENIED/);

    r = await fabric.submit('mls01op', 'DispatchBag', [bagId, sha(bagId, '50.5', 'salt-a'), rid()]);
    console.log('  PASS  DispatchBag tx', r.txId.slice(0, 16), 'status', r.result.status);

    await expectFail('duplicate dispatch', fabric.submit('mls01op', 'DispatchBag', [bagId, sha(bagId, '50.5', 'q'), rid()]), /INVALID_STATE/);
    await expectFail('other FPS cannot receive', fabric.submit('fps02op', 'ReceiveBag', [bagId, sha(bagId, '47.5', 'y'), rid()]), /ACCESS_DENIED/);

    const view = await fabric.evaluate('fps01op', 'GetBagStatus', [bagId]);
    if (JSON.stringify(view).includes('50.5')) throw new Error('weight leaked to FPS view!');
    console.log('  PASS  FPS view of bag contains no weight; status', view.result.status);

    r = await fabric.submit('fps01op', 'ReceiveBag', [bagId, sha(bagId, '47.5', 'salt-b'), rid()]);
    console.log('  PASS  ReceiveBag tx', r.txId.slice(0, 16), 'status', r.result.status);
    await expectFail('duplicate receipt', fabric.submit('fps01op', 'ReceiveBag', [bagId, sha(bagId, '47.5', 'z'), rid()]), /INVALID_STATE/);

    r = await fabric.submit('fps01op', 'RecordReconciliation', [bagId, 'FLAGGED', '3.0', '0.2', rid()]);
    console.log('  PASS  RecordReconciliation tx', r.txId.slice(0, 16), 'status', r.result.status);

    const hist = await fabric.evaluate('auditor1', 'GetTransactionHistory', [bagId]);
    console.log('  PASS  auditor history:', hist.result.map((h) => h.value.status).join(' -> '));
    console.log('\nALL FABRIC SMOKE TESTS PASSED');
    process.exit(0);
})().catch((e) => { console.error('FAILED:', fabric.describeError(e)); process.exit(1); });

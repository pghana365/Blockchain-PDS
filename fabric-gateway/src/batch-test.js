'use strict';
/**
 * Proves the orderer's block size: submits N transactions at the same moment and
 * reports how many ended up in each block.   node src/batch-test.js [N] [startNumber]
 * NOTE: creates N test bags (BAG-<start>..) on the ledger - use only on a demo network
 *       you are about to reset (reset-fabric.sh + scripts/reset_db.py).
 */
const crypto = require('node:crypto');
const fabric = require('./fabric');

const N = Number(process.argv[2] || 50);
const START = Number(process.argv[3] || 8001);

(async () => {
    const t0 = Date.now();
    const jobs = Array.from({ length: N }, (_, i) => {
        const bagId = `BAG-${START + i}`;
        return fabric.submit('pdsadmin', 'RegisterBag',
            [bagId, 'Rice', 'MLS-01', 'FPS-01', 'CONS-BATCHTEST', crypto.randomBytes(12).toString('hex')])
            .then((r) => ({ ok: true, block: r.blockNumber }), (e) => ({ ok: false, err: fabric.describeError(e) }));
    });
    const results = await Promise.all(jobs);
    const perBlock = {};
    let failed = 0;
    for (const r of results) {
        if (r.ok) perBlock[r.block] = (perBlock[r.block] || 0) + 1; else { failed++; console.log('  failed:', r.err.slice(0, 120)); }
    }
    console.log(`Submitted ${N} transactions concurrently in ${((Date.now() - t0) / 1000).toFixed(1)}s; failed: ${failed}`);
    for (const [b, c] of Object.entries(perBlock).sort((a, b) => a[0] - b[0])) console.log(`  block ${b}: ${c} transactions`);
    process.exit(failed ? 1 : 0);
})();

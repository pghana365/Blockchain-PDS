'use strict';

const crypto = require('node:crypto');
const express = require('express');
const fabric = require('./fabric');

const PORT = Number(process.env.GATEWAY_PORT || 3001);
const HOST = process.env.GATEWAY_HOST || '127.0.0.1'; // never exposed to the network by default
const API_KEY = process.env.GATEWAY_API_KEY || '';

if (API_KEY.length < 16) {
    console.error('GATEWAY_API_KEY must be set (min 16 chars). Copy .env.example to .env and set it.');
    process.exit(1);
}

const app = express();
app.use(express.json({ limit: '32kb' }));

// Only the Flask backend (holding the shared key) may call this service.
app.use((req, res, next) => {
    if (req.path === '/health') return next();
    const given = Buffer.from(req.get('x-api-key') || '');
    const want = Buffer.from(API_KEY);
    if (given.length !== want.length || !crypto.timingSafeEqual(given, want)) {
        return res.status(401).json({ error: 'unauthorized' });
    }
    next();
});

app.get('/health', (req, res) => res.json({ ok: true, channel: fabric.CHANNEL, chaincode: fabric.CHAINCODE }));

function handler(kind) {
    return async (req, res) => {
        const { identity, function: fn, args } = req.body || {};
        if (typeof identity !== 'string' || typeof fn !== 'string' || !Array.isArray(args)) {
            return res.status(400).json({ error: 'body must be {identity, function, args[]}' });
        }
        try {
            const out = kind === 'submit'
                ? await fabric.submit(identity, fn, args)
                : await fabric.evaluate(identity, fn, args);
            res.json(out);
        } catch (err) {
            const outcome = err.outcome || 'FAILED';
            // 504 = "we do not know whether it committed": the caller must ask the ledger, never assume failure
            const status = outcome === 'UNKNOWN' ? 504
                : (err.status || (/ACCESS_DENIED/.test(fabric.describeError(err)) ? 403 : 422));
            res.status(status).json({ error: fabric.describeError(err), outcome, txId: err.txId || null });
        }
    };
}

app.post('/tx-status', async (req, res) => {
    const { identity, txId } = req.body || {};
    if (typeof identity !== 'string' || typeof txId !== 'string') {
        return res.status(400).json({ error: 'body must be {identity, txId}' });
    }
    try {
        res.json(await fabric.txStatus(identity, txId));
    } catch (err) {
        res.status(err.status || 502).json({ error: fabric.describeError(err) });
    }
});

app.get('/identity/:name', (req, res) => {
    const info = fabric.identityInfo(req.params.name);
    res.status(info.exists ? 200 : 404).json(info);
});

app.post('/submit', handler('submit'));
app.post('/evaluate', handler('evaluate'));

app.listen(PORT, HOST, () => console.log(`PDS Fabric gateway listening on http://${HOST}:${PORT}`));

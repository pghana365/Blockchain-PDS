# PDS Fabric Gateway

Small Express service built on the official **`@hyperledger/fabric-gateway`** SDK. It is the only component that talks to
Hyperledger Fabric. The Flask backend calls it; browsers never do.

* Binds to `127.0.0.1:3001` and requires the `x-api-key` header (`GATEWAY_API_KEY` in `../.env`).
* Signs each transaction with the Fabric identity named in the request (`wallet/<id>/cert.pem` + `key.pem`, created by `fabric/scripts/enroll-identity.sh`).
* Only whitelisted chaincode functions are reachable (see `src/fabric.js`).

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness (no key needed) |
| `GET /identity/:name` | does this wallet identity exist, with its MSP/role/location (never returns keys) |
| `POST /submit` `{identity, function, args[]}` | endorse + submit + wait for commit -> `{txId, blockNumber, result}` |
| `POST /evaluate` `{identity, function, args[]}` | read-only query |

```powershell
npm install
npm start            # loads ../.env via node --env-file
node src\smoke-test.js   # live end-to-end chaincode test (creates one random BAG-xxxx on the ledger)
```

Peer endpoints default to `localhost:7051` (Org1) and `localhost:9051` (Org2) with TLS host override
`peer0.org{1,2}.example.com`; override with `ORG1_PEER_ENDPOINT` / `ORG2_PEER_ENDPOINT`. Restart the service after resetting the Fabric network.

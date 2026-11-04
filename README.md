# Blockchain-Based Blind Weight Verification for the Public Distribution System

Tracks individual rice bags from **Mandala Level Stock (MLS) -> Fair Price Shop (FPS)**.
Each bag has one permanent QR code. The MLS records the gross weight at dispatch; the FPS independently records its own gross
weight **without being able to see the MLS weight**; the system then reconciles the two against an admin-configurable tolerance
(initially 0.2 kg) and writes an auditable trail to a **Hyperledger Fabric** blockchain.

> A flagged bag means *"the two measurements differ by more than the tolerance - verify further"*. It is never treated as proof of theft or fraud,
> and the blockchain does not verify physical weight; humans still weigh the bags.

**Stack:** Hyperledger Fabric 2.5 (test-network, JavaScript chaincode, Fabric CA, Gateway SDK) · Node.js gateway service ·
Python Flask + SQLAlchemy + Alembic · MySQL 8 · Bootstrap 5 + vanilla JS · jsQR browser scanner · Docker / WSL2.

## Quick start

Full instructions: **[docs/setup-guide.md](docs/setup-guide.md)** · design and data placement: **[docs/architecture.md](docs/architecture.md)**.

```bash
# Ubuntu (WSL2) - once
ln -sfn "/mnt/c/Users/pghan/Documents/Github/Blockchain PDS" ~/pds
bash ~/pds/fabric/scripts/install-fabric.sh
bash ~/pds/fabric/scripts/reset-fabric.sh        # network + channel + chaincode + identities
```
```powershell
# Windows PowerShell, in the project folder - once
python scripts\make_env.py
python -m venv backend\.venv ; backend\.venv\Scripts\pip install -r backend\requirements.txt
cd fabric-gateway ; npm install ; cd ..
docker compose up -d mysql
cd backend ; .venv\Scripts\flask --app wsgi db upgrade ; .venv\Scripts\flask --app wsgi seed ; cd ..

# every session
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # then open http://localhost:5000
```

Demo users (all use `SEED_PASSWORD` from your `.env`): `admin`, `mls01.operator`, `mls02.operator`, `fps01.operator`, `fps02.operator`, `auditor1`.

## Roles

| Role | Can do | Cannot do |
|---|---|---|
| Admin | create users/locations, register bags, QR labels, set tolerance, view everything, record corrections | edit/delete dispatch, receipt or ledger records |
| MLS operator | see own MLS bags, scan, enter gross weight, dispatch, see own dispatch weight and final result | touch other MLS locations or any receipt; access discrepancy reports or transaction history |
| FPS operator | see incoming bags, scan, enter **own** weight, confirm receipt, then see the comparison | see the MLS weight/difference before submitting; touch other FPS bags; access discrepancy reports or transaction history |
| Auditor | read-only: all bags, weights, differences, tx IDs, history, audit log, integrity check | change anything |

## Where the pieces are

```
backend/            Flask app (models, services, routes, middleware, tests, migrations)
frontend/           templates + static (Bootstrap, jsQR vendored)
fabric-gateway/     Node.js Gateway SDK service + wallet (git-ignored keys)
fabric/chaincode/   pds-contract (JavaScript) + tests
fabric/scripts/     install / network / deploy / enrol / reset scripts (run in Ubuntu)
database/schema.sql MySQL schema snapshot       scripts/  env, reset, health check, e2e demo
docs/               architecture.md, setup-guide.md
```

## Tests (all run locally)

| Suite | Command | Result at last run |
|---|---|---|
| Backend: 12 required scenarios, security, page rendering, reliability/recovery, dashboard counters | `cd backend ; .venv\Scripts\python -m pytest -q` | **82 passed** (80 use SQLite + an in-memory *fake* ledger; 2 audit-concurrency tests need the docker MySQL and are skipped if it is not running) |
| Chaincode | `cd fabric\chaincode\pds-contract ; npm test` | **13 passed** (fake ledger stub, not a network) |
| Live Fabric via Gateway SDK | `cd fabric-gateway ; node src\smoke-test.js` | needs the running network; last run before the latest changes |
| Full scenario on the running stack (real Fabric) | `backend\.venv\Scripts\python scripts\e2e_demo.py --bag BAG-9001` | 23/23 checks (run on the latest code) |

There is **no automated real-Fabric test inside `pytest`**. Real-network behaviour (including a transaction whose commit status is
unknown) was verified by running scripts against the live network; see `docs/architecture.md` section 5b.

The complete live demonstration was verified locally on the running stack:

`50.5 kg dispatch -> 47.5 kg receipt -> 3.000 kg difference -> FLAGGED`

The end-to-end checks also verify QR generation, blind FPS access before receipt,
location authorization, duplicate-operation rejection, Fabric history, commitment
integrity, and the hash-chained audit log.

## Local verification status

The application is implemented and tested against the project requirements:

- Flask authentication, role-based access control, and location scoping
- Server-enforced blind weight verification for FPS operators
- MySQL persistence with Alembic migrations
- Hyperledger Fabric test-network integration through the official Gateway SDK
- JavaScript chaincode with identity, state-transition, and replay checks
- QR generation and browser scanning
- Admin configuration, corrections, reports, and auditor integrity checks

See [docs/setup-guide.md](docs/setup-guide.md) for the complete Windows,
WSL2, Docker, Fabric, database, and application startup procedure.

## Security notes

Never commit `.env` or `fabric-gateway/wallet/` (both git-ignored). Demo passwords and the Fabric test-network are for development only.
See the security and limitation sections in [docs/architecture.md](docs/architecture.md).

# Setup guide (Windows 10/11 + WSL2 Ubuntu + Docker Desktop)

Project folder used in this guide: `C:\Users\pghan\Documents\Github\Blockchain PDS`
(inside Ubuntu it is the symlink `~/pds`). Windows commands are **PowerShell**, Ubuntu commands are marked **Ubuntu**.

## 0. Prerequisites

| Tool | Where | Check |
|---|---|---|
| Docker Desktop (WSL2 backend), integration **enabled for Ubuntu** | Windows | `docker --version` in both PowerShell and Ubuntu |
| Ubuntu on WSL2 | Windows | `wsl --list --verbose` shows VERSION 2 |
| Node.js 20+ (Ubuntu) and 20/22/24 (Windows) | both | `node --version` |
| Python 3.11-3.13 | Windows | `python --version` |
| Git, curl, jq, build-essential | Ubuntu | |
| VS Code + "WSL" extension (optional) | Windows | |

Ubuntu packages (once):

```bash
# Ubuntu
sudo apt update && sudo apt install -y curl git build-essential python3-venv jq
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs
```

Docker Desktop -> Settings -> Resources -> WSL integration -> turn on **Ubuntu** -> Apply & restart.

## 1. One-time installation

```bash
# Ubuntu - link the Windows folder (space-free path) and install Fabric 2.5.16 (large download)
ln -sfn "/mnt/c/Users/pghan/Documents/Github/Blockchain PDS" ~/pds
bash ~/pds/fabric/scripts/install-fabric.sh
# if a download was interrupted (e.g. "Connection reset"), run:  bash ~/pds/fabric/scripts/install-fabric-retry.sh
```

```powershell
# Windows - project root
cd "C:\Users\pghan\Documents\Github\Blockchain PDS"
python scripts\make_env.py                     # creates .env with random secrets (never commit it)
python -m venv backend\.venv
backend\.venv\Scripts\pip install -r backend\requirements.txt
cd fabric-gateway; npm install; cd ..
cd fabric\chaincode\pds-contract; npm install; cd ..\..\..
```

## 2. Start the Fabric network, deploy the chaincode, enrol identities

```bash
# Ubuntu - one command (≈3-5 min). Destroys any previous ledger.
bash ~/pds/fabric/scripts/reset-fabric.sh
# or step by step:
bash ~/pds/fabric/scripts/network-up.sh              # official test-network + CAs + channel "pdschannel"
bash ~/pds/fabric/scripts/deploy-chaincode.sh        # package, install, approve (both orgs), commit "pds"
bash ~/pds/fabric/scripts/enroll-demo-identities.sh  # pdsadmin, mls01op, mls02op, fps01op, fps02op, auditor1
```

Add another identity later (e.g. a new MLS "MLS-03" in org 1):

```bash
bash ~/pds/fabric/scripts/enroll-identity.sh 1 mls03op mls MLS-03     # org, enrollment-id, role, location
```

## 3. Database

```powershell
docker compose up -d mysql                       # MySQL 8 on 127.0.0.1:3307 (port is MYSQL_HOST_PORT in .env)
cd backend
.venv\Scripts\flask --app wsgi db upgrade        # create tables from the Alembic migration
.venv\Scripts\flask --app wsgi seed              # roles, tolerance, sample MLS/FPS, 6 demo users
cd ..
```

The demo users all share the password stored as `SEED_PASSWORD` in `.env`:
`admin`, `mls01.operator`, `mls02.operator`, `fps01.operator`, `fps02.operator`, `auditor1`.

## 4. Run

Easiest: `powershell -ExecutionPolicy Bypass -File scripts\start.ps1` (starts Docker/MySQL/Fabric containers, then the gateway and Flask in two windows and runs a health check).

Manually, in two PowerShell windows:

```powershell
cd fabric-gateway ; npm start                    # http://127.0.0.1:3001  (needs ../.env)
cd backend ; .venv\Scripts\python wsgi.py        # http://127.0.0.1:5000
```

Open <http://localhost:5000>. Check everything: `backend\.venv\Scripts\python scripts\health_check.py`.

## 5. Tests

```powershell
cd backend ; .venv\Scripts\python -m pytest -q                 # 48 backend tests (SQLite + fake ledger)
cd fabric\chaincode\pds-contract ; npm test                    # 13 chaincode tests (fake ledger stub)
cd fabric-gateway ; node src\smoke-test.js                     # live Fabric test through the Gateway SDK (needs network)
backend\.venv\Scripts\python scripts\e2e_demo.py --bag BAG-0001   # full scenario over HTTP on the running stack
```

## 6. Demo script (the scenario from the brief)

1. Sign in as `admin` -> **Bags and QR codes** -> *Register bags* (MLS-01 -> FPS-01) -> `BAG-0001` is created and written to the blockchain. Click **QR** to print the label.
2. Sign in as `mls01.operator` (another browser/private window). **Scan QR code** (camera, upload an image of the label, or type `BAG-0001`). Enter **50.5** -> *Confirm dispatch* -> transaction ID shown, status `IN_TRANSIT`.
3. Sign in as `fps01.operator`. Scan the **same** QR. The page shows no weight (blind note). Enter **47.5** -> *Confirm receipt*.
4. Result: difference **3.000 kg** > tolerance **0.200 kg** -> **DISCREPANCY FLAGGED** with the "requires further verification" message; both MLS and FPS can see it.
5. Sign in as `auditor1` -> search `BAG-0001` -> *Audit report* -> **Verify against blockchain** and **Show on-chain state history**. Also see *Transaction history*, *Audit log*, *Discrepancy report*.
6. As `admin` -> *Settings* to change the tolerance; open the bag to record a **correction** (a new event; the original records stay untouched).

To repeat the demo from scratch: `bash ~/pds/fabric/scripts/reset-fabric.sh` (Ubuntu), restart the gateway window, then
`backend\.venv\Scripts\python scripts\reset_db.py --yes`. Always reset **both** the ledger and the database together.

## 7. Using a phone as the scanner

Camera access in a browser requires HTTPS or `localhost`. For a quick test use the *Upload QR image* button or manual entry.
For phone cameras put the app behind HTTPS (e.g. a reverse proxy/tunnel with a certificate) and set `PUBLIC_BASE_URL` in `.env` to that public URL
*before* generating labels (the URL is encoded in the QR).

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker` not found in Ubuntu | Docker Desktop -> WSL integration -> enable Ubuntu |
| MySQL "port is not available" | change `MYSQL_HOST_PORT` and the port in `DATABASE_URL` (3306 is blocked on some Windows hosts) |
| Gateway says `unknown Fabric identity` | run `enroll-demo-identities.sh` / `enroll-identity.sh` |
| Gateway TLS/connection errors after resetting Fabric | restart the gateway (it caches peer connections) |
| After reboot, Fabric does not respond | start Docker Desktop, then `scripts\start.ps1` (starts stopped containers) |
| "Blockchain gateway is unavailable" in the UI | start the gateway (`npm start` in `fabric-gateway`) |
| Ledger and database disagree after only one was reset | reset both (`reset-fabric.sh` + `reset_db.py`) |
| `fabric-ca-client` download fails with "Connection reset" | re-run `install-fabric-retry.sh` |

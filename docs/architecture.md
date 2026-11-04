# Architecture

**Project:** Blockchain-based blind weight verification and dispatch-receipt reconciliation for PDS rice bags (MLS -> FPS)
**Status:** implemented and tested locally (Hyperledger Fabric 2.5.16 test-network)

## 1. Components

```
 Browser (Bootstrap 5 + vanilla JS, jsQR scanner)
        |  HTTP, session cookie, CSRF token
        v
 Flask backend (Python)  --SQLAlchemy-->  MySQL 8  (exact weights, users, audit log)
        |  HTTP + API key (127.0.0.1 only)
        v
 Node.js Fabric Gateway service (@hyperledger/fabric-gateway)
        |  gRPC + TLS, signs as the operator's Fabric identity
        v
 Hyperledger Fabric test-network:  Org1 (MLS/admin)  Org2 (FPS/auditor)  orderer  3 CAs
        channel: pdschannel     chaincode: pds (JavaScript, fabric-contract-api)
```

| Folder | Purpose |
|---|---|
| `backend/` | Flask app: models, services (business rules), routes, middleware, tests, Alembic migrations |
| `frontend/` | Jinja templates + static CSS/JS (Bootstrap and jsQR vendored, no CDN) |
| `fabric-gateway/` | Thin REST wrapper over the official Gateway SDK; wallet of enrolled identities |
| `fabric/chaincode/pds-contract/` | Smart contract and its unit tests |
| `fabric/scripts/` | Install/start/deploy/enrol scripts around the official `test-network` |
| `database/` | `schema.sql` snapshot (generated) |

The Fabric runtime itself (binaries, docker images, test-network) lives in `~/fabric` inside Ubuntu/WSL2.
The project source stays on `C:` and is reachable from Ubuntu as `~/pds`.

## 2. Fabric design

* **Organisations:** `Org1MSP` represents the MLS side (and the system admin identity), `Org2MSP` the FPS side (and the auditor identity).
* **Identities:** issued by Fabric CA with certificate attributes `pds.role` (`admin|mls|fps|auditor`) and `pds.location` (`MLS-01`, `FPS-01`, ...). One identity per location (e.g. `mls01op`) is shared by that location's users; the Flask backend decides *which* identity a logged-in user signs with.
* **Endorsement:** default channel policy (majority of orgs) - every transaction is endorsed by both organisations' peers.
* **Chaincode functions:** `RegisterBag`, `DispatchBag`, `ReceiveBag`, `RecordReconciliation`, `RecordCorrection`, `GrantRole`, `RevokeRole`, `GetBagStatus`, `GetTransactionHistory`, `GetRole`.
* **Access control inside the chaincode** (not just in Flask):
  * the caller's `pds.role` attribute must be allowed for the function, and valid for the caller's MSP (`mls`/`admin` only on Org1MSP, `fps` only on Org2MSP);
  * `DispatchBag` requires `pds.location` = the bag's origin MLS; `ReceiveBag` requires `pds.location` = the bag's destination FPS;
  * reads (`GetBagStatus`, `GetTransactionHistory`) are limited to the operator's own bags; admin/auditor read everything;
  * identities can be revoked on the ledger with `RevokeRole`.
* **Block size:** the orderer is configured for **50 transactions per block** (`BatchSize.MaxMessageCount: 50`, set by `fabric/scripts/configure-batch.sh` before the channel is created).
  A block is cut when 50 transactions are waiting **or** after `BatchTimeout` (5 s) - so when users submit one at a time, a block holds fewer than 50 and
  each action waits up to 5 s to commit. `node fabric-gateway/src/batch-test.js 50` submits 50 at once and shows them landing in a single block.
  Override with `BATCH_COUNT` / `BATCH_TIMEOUT` env vars when running `network-up.sh`.
* **State machine:** `REGISTERED -> IN_TRANSIT -> RECEIVED -> MATCHED | FLAGGED`. Any out-of-order call is rejected, which also rejects duplicate dispatch/receipt.
* **Duplicate / replay protection:** every write carries a unique `requestId`, stored on the ledger; reuse is rejected.
* **Tamper evidence:** history is the Fabric blockchain itself (hash-linked blocks, endorsed by both orgs). `GetTransactionHistory` returns every state of a bag with its transaction ID. Nobody - including the admin - can edit past blocks; corrections are separate `RecordCorrection` events.

## 3. What is stored where

| Data | MySQL (off-chain) | Fabric ledger (on-chain) |
|---|---|---|
| Bag ID, commodity, consignment, origin MLS, destination FPS | yes | yes |
| Bag lifecycle status | yes | yes |
| **Exact dispatch weight (kg)** | **yes** (access-controlled) | **no** |
| **Exact receiving weight (kg)** | **yes** (access-controlled) | **no** |
| **Random salt per weight** (128-bit) | **yes** (never exposed by any API) | **no** |
| **Commitment** = SHA-256 of (bag ID, weight to 3 decimals, salt), for dispatch and for receipt | yes | **yes** |
| Reconciliation result (`MATCHED`/`FLAGGED`), difference, tolerance used | yes | yes |
| Transaction IDs, block numbers, who/when | yes | yes |
| Role grants / revocations, admin corrections (text) | yes | yes |
| Users, password hashes (scrypt), sessions, locations | yes | no |
| Application audit log (hash-chained rows) | yes | no |

Why: a permissioned ledger is visible to all peers of the channel, so putting the dispatch weight on it would reveal it
to the FPS organisation *before* it measures. A salted commitment hides the weight but still **binds** the MLS to it:
the weight cannot be changed later without breaking the commitment.

### What the blockchain proves - and what it does not

* It proves **who recorded what, when, in what order**, and that the records were **not altered afterwards**
  (the *Verify against blockchain* button recomputes every commitment from MySQL and compares it with the ledger).
* It does **not** prove that a physical weighing was accurate. Scales, operators and transport are still human.
  A flagged bag is a quantity difference that needs further verification - never an automatic finding of theft or fraud.
* Trust assumption: the Flask backend computes the difference (it is the only component that holds both weights before
  they are revealed). The chaincode checks that the claimed result is consistent with the claimed difference/tolerance
  and that the bag is in the right state, but it cannot recompute the difference from hidden values. The integrity
  check lets an auditor re-derive it independently.

## 4. Blind verification - how it is enforced

The restriction is in the **server**, in layers:

1. `services/access.py` - the only way to load a weight row is `visible_dispatch / visible_receipt / visible_reconciliation`.
   For an FPS user, `visible_dispatch` returns `None` unless that FPS's own receipt already exists (checked with an ID-only query).
2. `models/bag.py` has **no ORM relationship** from `Bag` to the weight tables, so a template or API cannot lazy-load a weight by accident.
3. `services/serializers.py` builds every API/page view from the `visible_*` results: for an FPS operator before receipt the
   JSON simply has no `dispatch`, `receipt` or `reconciliation` keys.
4. List queries for FPS (`reports.fps_incoming`) join no weight table; FPS discrepancy/transaction pages are scoped in SQL by `fps_id`.
5. Row-level scoping everywhere (`access.bags_query_for`, `Bag.mls_id/fps_id` filters) - another location's bags are returned as *404*.
6. Test `test_03_fps_cannot_see_mls_weight_before_receipt` scans the FPS API response and every FPS-facing page for the weight.

## 5. Request flows

MySQL and Fabric cannot be one atomic transaction, so every bag write (register, dispatch, receive, reconciliation) is a **write-ahead
saga** in `services/workflow.py`:

1. **Intent (one MySQL commit):** authenticate -> role/location check -> validate -> lock the bag row -> save the record (its `tx_id` stays empty)
   and a `blockchain_transactions` row with status `PENDING`. A crash after this point loses nothing.
2. **Send** the transaction as the operator's Fabric identity and get its **verified** outcome (`ledger.submit_tx`).
3. **Settle (one MySQL commit)**, idempotent:
   * `COMMITTED` (ledger said VALID) -> fill in `tx_id`/block, advance the bag state, audit-log.
     For a receipt this **also creates the reconciliation row in the same commit**, so a bag can no longer be left in `RECEIVED`.
   * `FAILED` (rejected at endorsement or invalidated at commit - certainly not on the ledger) -> the intended record is removed again.
   * `UNKNOWN` (timeout / lost connection: the commit status was never seen) -> **nothing is rolled back and nothing is resent.**
     The record stays `PENDING`, the API answers **202 `PENDING_CONFIRMATION`** (never "success"), and further actions on that bag are refused with 409.

**Resolving unknown outcomes** (`resolve_pending`): runs automatically when the bag is viewed, before the next write on it, and from the admin
button / `POST /api/admin/bags/<id>/resolve-pending`. It asks the ledger, first by transaction id (`/tx-status`, which reads the peer's `qscc`
system chaincode: VALID / INVALID / NOT_FOUND) and, if the id was never received, by comparing the **on-chain bag state** with the saved record
(the weight commitment is unique per attempt). Confirmed -> completed; INVALID -> undone; not on the ledger -> stays pending.
Only an administrator can **abandon** a pending operation, and only if it is older than `PENDING_ABANDON_MINUTES` (default 15) *and* the ledger
positively does not have it. Delayed block commits are absorbed by a short poll (`LEDGER_CONFIRM_POLLS` x `LEDGER_CONFIRM_INTERVAL`) before an
operation is reported as pending.

**Blind rule during recovery:** the FPS sees the MLS weight only once its **own receipt is confirmed on the ledger** (`access.has_confirmed_receipt`,
i.e. `receipt_records.tx_id` is set), because an unconfirmed receipt can still be undone and re-entered.

**Reconciliation on-chain write** (`RecordReconciliation`) follows the same pattern; `POST /api/admin/bags/<id>/retry-chain-sync` is idempotent:
it rebuilds a missing reconciliation row from the original stored weights (never changing existing rows), checks the ledger for a result that is
already there, and otherwise writes it once.

Other writes (`GrantRole`, `RecordCorrection`) use the simpler path: on an unknown outcome the local change is not saved and a `PENDING`
transaction row is kept, with an error telling the user the outcome is unknown.

### 5b. How this was verified
* Unit tests use an in-memory **fake** ledger that models on-chain state and the outcome cases (`backend/tests/test_reliability.py`).
* Against the **real Fabric network**: a second gateway with a 300 ms commit wait (`COMMIT_STATUS_TIMEOUT_MS`, default 60 000) produces genuine
  "unknown" outcomes for register, dispatch, receive and reconciliation; each was then completed from the ledger. The decoded blocks show exactly one
  transaction per operation (no duplicates).
* `audit_chain_head` (below) was tested with 40 concurrent requests against real MySQL.

**Audit chain:** `audit_chain_head` is a single row holding the newest entry hash. `audit.log()` locks it (`SELECT ... FOR UPDATE`) before reading the
previous hash, so concurrent requests queue instead of sharing a `prev_hash`. Entry format and hash calculation are unchanged.
Lock order everywhere is bag row -> `blockchain_transactions` row -> audit head, so `audit.log()` must be the last thing before a commit.

**QR scan:** QR = `http://<PUBLIC_BASE_URL>/b/BAG-0001` (nothing else). Not logged in -> `/login?next=...` -> back to the bag. The same URL renders the
dispatch form for the right MLS operator, the blind receipt form for the right FPS operator, the read-only audit report for the auditor.

## 6. Security controls

* Passwords: scrypt with per-password salt; policy (>=10 chars, upper/lower/digit); lock for 15 min after 5 failures; constant-ish time for unknown users.
* Sessions: Flask-Login, HttpOnly + SameSite=Lax cookies, `session_protection=strong`, 30 min lifetime, session cleared on login (fixation), disabled users logged out immediately.
* CSRF: Flask-WTF on all POST/PUT/PATCH (token in a meta tag, sent as `X-CSRFToken`).
* CSP: `script-src 'self'; style-src 'self'` - no inline scripts or styles; `X-Frame-Options: DENY`; `no-store` caching.
* SQL injection: SQLAlchemy parameterised queries only; inputs validated (bag-ID regex, weight parser with Decimal, length limits).
* Open redirect: `next` accepts only same-site relative paths.
* Gateway: bound to 127.0.0.1, API key (timing-safe compare), function whitelist, wallet private keys never leave the machine and are git-ignored.
* Audit: every login, view, denial, rejection, admin action and ledger error is written to `audit_logs`; entries are hash-chained so edits/deletions are detectable.

## 7. Known limitations (prototype)

* Fabric `test-network` is a single-machine development network (one peer per org, solo-like ordering via Raft single node, test passwords).
* One shared Fabric identity per location; disabling an individual web user does not revoke the shared Fabric certificate.
* The Flask development server is used; use Gunicorn/Waitress behind HTTPS for anything real. Browser camera scanning on a phone requires HTTPS.
* MySQL is the system of record for exact weights - an attacker with direct DB access can alter weights, but cannot do so *undetected*
  (commitments on the ledger and the audit-log hash chain expose it).

# Code review: Blockchain-Based PDS Weight Verification

**Date:** 2026-11-04 · **Reviewed:** git `HEAD` (beb8a37, including the two later commits that hid discrepancy reports and transaction history from operators) plus uncommitted block-size changes
**Method:** read the code; ran 52 backend + 13 chaincode tests (all pass); ran a live probe against the running stack (Flask, gateway, MySQL, Fabric 2.5.16); decoded the blocks directly from the peer.
**Evidence tags:** **[live]** reproduced on the running system · **[code]** evident from the source, not reproduced · **[not tested]**

> **Update after the fixes (2026-11-04):** bugs 1-3 and B1 below are fixed (audit-chain race, receipt/reconciliation consistency,
> ambiguous commit outcome, MLS counters) and the test counts are corrected: 82 backend + 13 chaincode tests. See
> `docs/architecture.md` section 5. Everything else in this report is unchanged and still open.

## 0. The two questions you asked first

### Can the FPS retrieve the MLS dispatch weight before submitting its own? **No. [live]**
With BAG-0001 `IN_TRANSIT` (MLS weight 50.5 kg) I logged in as `fps01.operator` and called 13 endpoints (bag API, bag page, dashboard, transactions, discrepancy report, audit log, audit verify/history, admin pages, QR, scan-resolve, `/bags/…` alias, label sheet):
the 6 permitted ones contained no weight, the 7 restricted ones returned 403. The same user's *ledger* view (`GetBagStatus` as `fps01op`) has keys `…dispatchCommitment, dispatchTxId…` but **no plaintext weight** (only a SHA-256 commitment). Another FPS (`fps02`) got 404/403. The gateway without its API key returned 401.
Why it holds: weight rows are only reachable through `services/access.py::visible_*`; `Bag` has no ORM relationship to the weight tables; list queries for FPS join no weight table; and the ledger never contains weights. Residual (by design): anyone with MySQL access, the `.env`/gateway key, or admin/auditor rights can read weights.

### Are genuine Hyperledger Fabric transactions recorded, or only MySQL rows? **Genuine Fabric. [live]**
Read straight from `peer0.org1` (no app, no MySQL): channel height 13; blocks 6–12 contain `RegisterBag ×3, DispatchBag ×2, ReceiveBag, RecordReconciliation`, each with **2 endorsements** (Org1 + Org2); all four transaction IDs the app displayed were found inside those blocks; each block stores the previous block's hash. Only one of two simultaneous dispatches of the same bag reached the ledger. A plain Org1 admin certificate without `pds.role` is refused by the chaincode (`ACCESS_DENIED`).

## 1. Implemented and verified

| Scope item | Evidence |
|---|---|
| Permissioned Fabric, MLS→FPS, 2 orgs, CA identities with `pds.role`/`pds.location` | blocks decoded above; chaincode refuses identities without attributes |
| Individual bag tracking + state machine | tests; live run REGISTERED→IN_TRANSIT→RECEIVED→FLAGGED; duplicate dispatch → 409 |
| Unique QR per bag (PNG generation) | PNG verified; one URL per bag used by both MLS and FPS **[camera scan not tested]** |
| Role + location access control | 52 tests; live probes; other-location bags return 404 |
| MLS dispatch weight / FPS independent weight / blind verification | section 0 |
| Configurable tolerance, automatic discrepancy detection | 50.5 vs 47.5 → difference 3.000 > 0.200 → FLAGGED, with "further verification" text; tolerance snapshot stored per reconciliation |
| Blockchain transaction history + integrity check | auditor verify (10 checks) and on-chain state history passed earlier on the live network |
| MySQL, Flask, JS Gateway SDK integration | all exercised end to end |
| Dashboards | MLS and FPS pages seen in a real browser; admin/auditor pages only through automated render tests |

## 2. Partially implemented

| Item | Gap |
|---|---|
| "Permanent" QR | the code encodes `PUBLIC_BASE_URL` (now `http://localhost:5000`); printed labels die if the host changes **[code]** |
| Reconciliation on-chain write | retried by admin only if a reconciliation row exists; there is a window where it does not (see 9) |
| Audit-log hash chain | breaks under concurrent requests (see 9) |
| Admin user management | create/disable/reset-password/unlock exist; role change absent; location change exists in the API but has no UI |
| Corrections | recorded on-chain and in MySQL, but never shown in reports or reflected anywhere else |
| Auditor "all bags" | listing comes from MySQL only; the chain cannot be enumerated (no range query in the chaincode) |
| `GrantRole` | advisory: the chaincode enforces *revocation* only; grants are not an allow-list **[code]** |
| Operator transaction visibility | operators no longer see transaction tables (your commits). They still get the tx ID after dispatch/receipt and in the dispatch/receipt cards, so "view dispatch transaction status" is met |

## 3. Missing

Password change / forced first-login change · pagination (lists silently capped at 500) · CSV/PDF export of discrepancy or audit reports · full ledger-vs-database reconciliation job · block-event listener · automated tests against a real Fabric network (backend tests use a fake ledger; the real path is only the manual `e2e_demo.py`/probe) · backup/restore · production server + HTTPS · one-command start for the Fabric side after reboot beyond `start.ps1` · continuous multi-bag scan mode.

## 4. Security issues

| # | Issue | Severity | Tag |
|---|---|---|---|
| S1 | **User enumeration:** after repeated bad logins a real account answers "Account temporarily locked…", an unknown one "Invalid username or password" | Medium | live |
| S2 | All six seeded users share one password; no forced change, no self-service change | Medium (demo), High (real) | code |
| S3 | Lockout is per-account only; no per-IP rate limit, so one IP can lock out any account (denial of service) | Medium | code |
| S4 | Session cookie has no `Secure` flag, no HSTS; app served over plain HTTP on the Flask dev server | Medium | live |
| S5 | MySQL app account has `ALL PRIVILEGES`, so `audit_logs`, `blockchain_transactions`, weights can be UPDATE/DELETEd; no DB-level immutability | Medium | live |
| S6 | One shared Fabric identity per location; disabling a web user does not revoke the certificate | Low–Medium | code |
| S7 | Fabric private keys live in `fabric-gateway/wallet` inside the project on `C:` (git-ignored, but readable by anyone on the PC) | Low (demo) | code |
| S8 | The chaincode cannot recompute the difference (weights are hidden); a compromised backend could register a false `MATCHED`/`FLAGGED` that is consistent with the difference it also supplies | Medium, inherent to the design; mitigated by the integrity check | code |
| S9 | Dependencies: `npm audit` 0 vulnerabilities (gateway, chaincode). Flask-WTF 1.2.2→1.3.0 and SQLAlchemy 2.0.55→2.1.8 are available (not security fixes) | Info | live |

## 5. Blockchain integration issues

| # | Issue | Severity | Tag |
|---|---|---|---|
| B1 | **Ambiguous commit timeout.** The gateway waits 60 s for commit status; on a timeout the backend treats the call as failed and rolls MySQL back, although the transaction may still commit. MySQL would then say REGISTERED while the ledger says IN_TRANSIT, and every retry fails with `INVALID_STATE` | High | code |
| B2 | Latency after the 50-per-block change: block timeout 5 s ⇒ dispatch **5.1 s**, receipt **10.2 s** (two transactions), registering 3 bags **15.3 s** (sequential). Registering 50 bags would take about 4 minutes in one HTTP request | High for demo | live |
| B3 | No single call is atomic between MySQL and Fabric; the process relies on rollback + retry (good for failures *before* submit, see B1 for after) | Medium | code |
| B4 | No tool to compare *every* MySQL bag with the ledger or to import chain state into MySQL | Medium | code |
| B5 | Single peer per org, single orderer node, test-network credentials: fine for a demo, not "consortium" | Info | live |
| B6 | Reconciliation correctness is trusted to the backend (see S8) | Medium | code |

## 6. Database consistency issues

* **No CHECK constraints** (weight > 0, status values, operator must have exactly one matching location) and `bags.status` is free text; consistency is enforced only in Python **[code]**.
* `dispatch_records.mls_id` / `receipt_records.fps_id` are not constrained to equal the bag's MLS/FPS **[code]**.
* Receipt and reconciliation are separate commits (see 9, bug 2) **[code]**.
* `next_bag_id()` scans every bag ID and races between two admins (unique constraint stops duplicates but surfaces as a 500) **[code]**.
* Row locking works: simultaneous dispatch of one bag gave exactly one 201 and one 409 **[live]**. The lock is held across the 5–10 s Fabric call, which serialises work on that bag **[code]**.
* Good: `UNIQUE(bag_pk)` on dispatch/receipt/reconciliation, unique `request_id`, foreign keys, indexes, Decimal(8,3) weights, migration and `schema.sql` load cleanly.

## 7. Frontend usability

1. Waiting 5–10 s with only the button text changing to "Submitting to blockchain…"; no progress or step display.
2. Native browser `confirm()` dialogs for the critical action; small touch targets, no large confirm screen showing bag/weight.
3. After each dispatch the user must click "Scan another"; no continuous mode for loading a truck.
4. MLS dashboard counters are wrong when a status filter is used (see 9, bug 3).
5. Tables are capped at 500 rows with no paging or sorting; no CSV export.
6. No user-role/location editing screen; no password-change page.
7. The admin/auditor dashboards were not viewed in a browser in this review (render tests only).

## 8. Mobile QR scanning

All **[code]** unless noted; **no camera test was possible here.**
1. Printed QR codes contain `http://localhost:5000/b/BAG-xxxx`: a phone scanning them opens *its own* localhost.
2. Flask listens on `127.0.0.1` only, so a phone on the Wi-Fi cannot reach the app at all.
3. In-page camera scanning needs a secure context (HTTPS or localhost); over `http://<LAN-IP>` Chrome/Safari refuse `getUserMedia`. The phone's *built-in camera app* does not have this limit and just opens the link.
4. jsQR decodes at full camera resolution on every animation frame (no downscaling or throttling): likely slow/hot on low-end phones.
5. No torch/zoom control, no fallback message for iOS "in-app" browsers.
6. `Upload QR image` and manual entry work as fallbacks.

## 9. Important bugs (reproduced unless noted)

1. **Audit chain reports "BROKEN" after normal concurrent use. [live]** 24 concurrent failed logins produced `{"ok": false, "first_bad_entry": 28}`. `audit.log()` reads the last hash then writes (`audit.py:28-29`), so two requests share one `prev_hash`. The auditor dashboard and audit-log page then show a red "BROKEN" banner during a demo, a false alarm that undermines the feature.
2. **Stuck `RECEIVED` bag. [code]** `receive_bag` commits the receipt (`workflow.py:181`) and only then computes the reconciliation. If the process or MySQL fails in between, the bag stays `RECEIVED` forever with no reconciliation row; the admin "retry sync" returns 404 `No reconciliation to sync` (`api_admin.py:231`).
3. **MLS dashboard counters wrong under a filter. [live]** With `?status=FLAGGED`, "Awaiting dispatch" showed 0 while BAG-0003 was still REGISTERED, because counts are taken from the filtered rows.
4. **Ambiguous timeout divergence** (B1). [code]
5. Docs drift: README says 48 backend tests (now 52); `e2e_demo.py` was last run before your two commits (the probe re-ran the same flow on HEAD, but the script itself should be re-run). [live]

## 10. Recommended modifications, in priority order

**Smallest set to make the project reliable and demonstrable (do these):**

| # | Change | Size | Fixes |
|---|---|---|---|
| 1 | **Serialise the audit chain** (lock a one-row "chain head" or use `GET_LOCK` inside `audit.log`) + a thread test | ~20 lines | bug 1 |
| 2 | **Make receipt + reconciliation one DB commit** (insert the reconciliation row before committing the receipt; only the Fabric write of the result stays afterwards) + "recompute" fallback in the retry button | ~30 lines | bug 2 |
| 3 | **Handle ambiguous Fabric outcomes**: gateway reports "commit status unknown" distinctly; backend then reads the bag's on-chain state before deciding to roll back or finish the DB update | ~40 lines | B1 |
| 4 | **Decide the block-timeout trade-off and add a concurrent bulk path.** `MaxMessageCount 50` stays; lower `BatchTimeout` to 1–2 s (dispatch ≈2 s, receipt ≈4 s) and make multi-bag registration submit in parallel so 50 registrations really fall into one block (this is the demonstration of "50 per block") | ~30 lines + config | B2 |
| 5 | **Login hardening**: same message for locked/unknown accounts, per-IP throttle, forced password change on first login (seed users flagged) | ~60 lines | S1–S3 |
| 6 | **Phone demo setup**: bind `0.0.0.0` (firewall to LAN), set `PUBLIC_BASE_URL=http://<LAN-IP>:5000`, print labels *after* that, use the phone's native camera to open the link (HTTPS only if you want in-page scanning) | config only | mobile 1–3 |
| 7 | Fix MLS counters (compute from unfiltered counts) and refresh README test count | ~5 lines | bug 3, 5 |
| 8 | Add one automated real-Fabric integration test (`pytest -m fabric`, wraps the existing e2e script) so the backend↔gateway↔chaincode path is covered in the test suite | small | test gap |

**Next tier (after the above):** DB CHECK constraints and revoking UPDATE/DELETE on audit tables from the app account; pagination + CSV export; ledger-vs-database full reconciliation command (needs a range query in the chaincode); in-page scanner downscaling/torch; production WSGI server + HTTPS; positive allow-list semantics for `GrantRole`; per-user Fabric identities.

**Not worth doing for the scope:** multi-orderer/multi-peer network, Fabric private data collections (the commitment design already meets the privacy requirement), an SPA rewrite.

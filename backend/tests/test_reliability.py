"""Receipt/reconciliation consistency, ambiguous Fabric outcomes, and dashboard counters.

These use the in-memory FakeLedger (see conftest) - they test the application's decisions, not Fabric.
The same scenarios against a real network were run separately (see the final report)."""
import re

import pytest
import requests

from app.extensions import db
from app.models import (AuditLog, Bag, BlockchainTransaction, DispatchRecord, FpsLocation, MlsLocation,
                        ReceiptRecord, ReconciliationRecord)
from app.services import audit, workflow
from app.services.ledger import FAILED, UNKNOWN, GatewayClient, LedgerError

from conftest import dispatch, make_client, receive


def ops(bag_id="BAG-0001"):
    bag = Bag.query.filter_by(bag_id=bag_id).one()
    return BlockchainTransaction.query.filter_by(bag_pk=bag.id).order_by(BlockchainTransaction.id).all()


def count_calls(ledger, fn):
    return len([c for c in ledger.calls if c["function"] == fn])


# =========================================================== PHASE 2 - receipt <-> reconciliation
def test_receipt_and_reconciliation_are_saved_together(app, clients, bag):
    dispatch(clients, bag, "50.5")
    r = receive(clients, bag, "47.5")
    assert r.status_code == 201 and r.get_json()["outcome"] == "CONFIRMED"
    b = Bag.query.one()
    rec, recon = ReceiptRecord.query.one(), ReconciliationRecord.query.one()
    assert rec.tx_id and recon.status == "FLAGGED" and b.status == "FLAGGED"     # never left in RECEIVED
    assert str(recon.dispatch_weight_kg) == "50.500" and str(recon.receipt_weight_kg) == "47.500"


def test_crash_after_ledger_commit_is_recovered_without_duplicates(app, clients, bag, ledger, monkeypatch):
    """Process dies after Fabric committed the receipt but before MySQL was updated."""
    dispatch(clients, bag, "50.5")
    real_settle = workflow._settle

    def crash(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(workflow, "_settle", crash)
    with pytest.raises(RuntimeError):
        receive(clients, bag, "47.5")
    monkeypatch.setattr(workflow, "_settle", real_settle)
    db.session.rollback()

    # state after the crash: receipt saved but unconfirmed, ledger already has it, no reconciliation yet
    assert ReceiptRecord.query.one().tx_id is None and ReconciliationRecord.query.count() == 0
    assert ops()[-1].function == "ReceiveBag" and ops()[-1].status == "PENDING"
    assert ledger.chain[bag]["status"] == "RECEIVED"

    # While the receipt is unconfirmed the blind rule still hides the MLS weight from the FPS
    from app.models import User
    from app.services import access
    fps_user = User.query.filter_by(username="fps01.operator").one()
    assert access.visible_dispatch(fps_user, Bag.query.one()) is None

    # Viewing the bag settles the operation against the ledger: receipt + reconciliation, once
    d = clients["fps1"].get(f"/api/bags/{bag}").get_json()
    assert d["reconciliation"]["status"] == "FLAGGED" and d["receipt"]["weight_kg"] == "47.500"
    assert ReceiptRecord.query.count() == 1 and ReconciliationRecord.query.count() == 1
    assert ReceiptRecord.query.one().tx_id
    assert [o.status for o in ops() if o.function == "ReceiveBag"] == ["COMMITTED"]
    assert count_calls(ledger, "ReceiveBag") == 1                       # nothing was resent to Fabric
    assert receive(clients, bag, "47.5").status_code == 409             # a user retry is a duplicate


def test_stuck_received_bag_is_repaired_by_admin_retry_and_is_idempotent(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    ledger.fail_functions.add("RecordReconciliation")
    assert receive(clients, bag, "47.5").status_code == 201
    ledger.fail_functions.clear()
    # reproduce the reported bug state: receipt confirmed, bag RECEIVED, reconciliation row missing
    ReconciliationRecord.query.delete()
    Bag.query.one().status = "RECEIVED"
    db.session.commit()

    a = clients["admin"]
    r1 = a.post(f"/api/admin/bags/{bag}/retry-chain-sync")
    assert r1.status_code == 200, r1.get_json()           # used to be 404 "No reconciliation to sync"
    recon = ReconciliationRecord.query.one()
    assert recon.status == "FLAGGED" and str(recon.difference_kg) == "3.000" and recon.chain_status == "CONFIRMED"
    assert str(recon.dispatch_weight_kg) == "50.500" and str(recon.receipt_weight_kg) == "47.500"   # originals kept
    assert Bag.query.one().status == "FLAGGED"
    calls_before = count_calls(ledger, "RecordReconciliation")

    r2 = a.post(f"/api/admin/bags/{bag}/retry-chain-sync")             # idempotent
    assert r2.status_code == 200
    assert ReconciliationRecord.query.count() == 1 and count_calls(ledger, "RecordReconciliation") == calls_before


def test_retry_without_a_confirmed_receipt_is_refused_cleanly(app, clients, bag):
    r = clients["admin"].post(f"/api/admin/bags/{bag}/retry-chain-sync")
    assert r.status_code == 409 and r.get_json()["code"] == "NOTHING_TO_RECONCILE"
    assert ReconciliationRecord.query.count() == 0


def test_reconciliation_lost_response_is_found_on_ledger_not_resent(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    ledger.mode["RecordReconciliation"] = "unknown_lost_committed"       # committed, but no answer and no tx id
    assert receive(clients, bag, "47.5").status_code == 201
    assert ReconciliationRecord.query.one().chain_status == "PENDING"
    assert ledger.chain[bag]["reconciliation"]["result"] == "FLAGGED"
    ledger.mode.clear()
    assert clients["admin"].post(f"/api/admin/bags/{bag}/retry-chain-sync").status_code == 200
    assert ReconciliationRecord.query.one().chain_status == "CONFIRMED"
    assert count_calls(ledger, "RecordReconciliation") == 1              # the ledger already had it: nothing resent


def test_blind_rule_holds_while_receipt_is_unconfirmed(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    ledger.mode["ReceiveBag"] = "unknown_pending"
    r = receive(clients, bag, "47.5")
    assert r.status_code == 202 and r.get_json()["outcome"] == "PENDING_CONFIRMATION"
    text = r.get_data(as_text=True)
    assert "50.5" not in text and "dispatch" not in r.get_json()["bag"] and "reconciliation" not in r.get_json()["bag"]
    page = clients["fps1"].get(f"/b/{bag}").get_data(as_text=True)
    assert "50.5" not in page and "Waiting for blockchain confirmation" in page
    # FPS cannot re-submit a different weight to "try again" - and still has not seen the MLS weight
    assert receive(clients, bag, "50.5").status_code == 409
    ledger.commit_pending()                                              # block arrives
    d = clients["fps1"].get(f"/api/bags/{bag}").get_json()
    assert d["dispatch"]["weight_kg"] == "50.500" and d["reconciliation"]["status"] == "FLAGGED"


# =========================================================== PHASE 3 - ambiguous Fabric outcomes
def test_unknown_but_committed_is_verified_not_rolled_back(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_committed"
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 201 and r.get_json()["outcome"] == "CONFIRMED"      # confirmed via tx status, not assumed
    assert ledger.tx_status_calls >= 1
    assert Bag.query.one().status == "IN_TRANSIT" and DispatchRecord.query.one().tx_id
    assert count_calls(ledger, "DispatchBag") == 1
    assert [o.status for o in ops()] == ["COMMITTED", "COMMITTED"]


def test_delayed_block_commit_is_waited_for(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_pending"
    ledger.commit_after_polls = 2                                              # visible on the 2nd status poll
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 201 and Bag.query.one().status == "IN_TRANSIT"
    assert ledger.tx_status_calls == 2


def test_unconfirmed_is_kept_pending_blocks_retries_and_completes_later(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_pending"
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 202 and r.get_json()["outcome"] == "PENDING_CONFIRMATION"
    assert "success" not in r.get_json()["message"].lower()
    # NOT rolled back, NOT marked successful
    assert Bag.query.one().status == "REGISTERED"
    d = DispatchRecord.query.one()
    assert d.tx_id is None and str(d.weight_kg) == "50.500"
    assert ops()[-1].function == "DispatchBag" and ops()[-1].status == "PENDING" and ops()[-1].tx_id
    # a retry must not create a second Fabric transaction
    again = dispatch(clients, bag, "50.5")
    assert again.status_code == 409 and again.get_json()["code"] == "PENDING_CONFIRMATION"
    assert DispatchRecord.query.count() == 1 and len(ledger.in_flight) == 1 and count_calls(ledger, "DispatchBag") == 0
    # the UI/API tell the user what is going on
    view = clients["mls1"].get(f"/api/bags/{bag}").get_json()
    assert view["pending"] and view["can_dispatch"] is False and view["status"] == "REGISTERED"
    # the block finally arrives
    ledger.commit_pending()
    view = clients["mls1"].get(f"/api/bags/{bag}").get_json()                  # viewing settles it
    assert view["status"] == "IN_TRANSIT" and not view["pending"]
    assert DispatchRecord.query.one().tx_id and ops()[-1].status == "COMMITTED" and ops()[-1].block_number
    assert count_calls(ledger, "DispatchBag") == 1


def test_unknown_without_tx_id_is_resolved_from_onchain_state(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_lost_committed"    # HTTP timeout: committed, but we never got the tx id
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 202 and Bag.query.one().status == "REGISTERED"
    ledger.mode.clear()
    view = clients["mls1"].get(f"/api/bags/{bag}").get_json()
    assert view["status"] == "IN_TRANSIT" and DispatchRecord.query.one().tx_id == ledger.chain[bag]["dispatchTxId"]
    assert count_calls(ledger, "DispatchBag") == 1


def test_lost_submission_stays_pending_until_admin_abandons_after_the_waiting_period(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_lost"
    assert dispatch(clients, bag, "50.5").status_code == 202
    ledger.mode.clear()
    assert clients["mls1"].get(f"/api/bags/{bag}").get_json()["pending"]       # ledger doesn't have it, but it is NOT undone
    a = clients["admin"]
    # too early to abandon
    left = a.post(f"/api/admin/bags/{bag}/resolve-pending", json={"abandon": True}).get_json()["still_pending"]
    assert len(left) == 1 and DispatchRecord.query.count() == 1
    # only admins may even ask
    assert clients["mls1"].post(f"/api/admin/bags/{bag}/resolve-pending", json={"abandon": True}).status_code == 403
    app.config["PENDING_ABANDON_MINUTES"] = 0
    left = a.post(f"/api/admin/bags/{bag}/resolve-pending", json={"abandon": True}).get_json()["still_pending"]
    assert left == [] and DispatchRecord.query.count() == 0 and Bag.query.one().status == "REGISTERED"
    assert AuditLog.query.filter_by(action="PENDING_ABANDON_REQUESTED").count() == 2   # both requests are audited
    assert dispatch(clients, bag, "50.5").status_code == 201                    # can be done again


def test_abandon_is_refused_if_the_ledger_has_the_transaction(app, clients, bag, ledger):
    app.config["PENDING_ABANDON_MINUTES"] = 0
    ledger.mode["DispatchBag"] = "unknown_lost_committed"
    assert dispatch(clients, bag, "50.5").status_code == 202
    ledger.mode.clear()
    r = clients["admin"].post(f"/api/admin/bags/{bag}/resolve-pending", json={"abandon": True})
    assert r.status_code == 200 and r.get_json()["still_pending"] == []
    assert Bag.query.one().status == "IN_TRANSIT" and DispatchRecord.query.count() == 1   # completed, not undone


def test_ledger_unreachable_changes_nothing(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_pending"
    assert dispatch(clients, bag, "50.5").status_code == 202
    ledger.unreachable = True
    ledger.commit_pending()
    clients["mls1"].get(f"/api/bags/{bag}")
    assert Bag.query.one().status == "REGISTERED" and ops()[-1].status == "PENDING"
    ledger.unreachable = False
    assert clients["mls1"].get(f"/api/bags/{bag}").get_json()["status"] == "IN_TRANSIT"


def test_invalidated_transaction_is_undone_and_reported_as_failed(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "invalid"
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 409
    assert DispatchRecord.query.count() == 0 and Bag.query.one().status == "REGISTERED"
    assert ops()[-1].status == "FAILED" and "rejected" in ops()[-1].error.lower()
    ledger.mode.clear()
    assert dispatch(clients, bag, "50.5").status_code == 201


def test_definite_failure_before_submission_is_rolled_back(app, clients, bag, ledger):
    ledger.fail_functions.add("ReceiveBag")
    dispatch(clients, bag, "50.5")
    assert receive(clients, bag, "47.5").status_code == 503
    assert ReceiptRecord.query.count() == 0 and Bag.query.one().status == "IN_TRANSIT"
    assert ops()[-1].status == "FAILED"
    ledger.fail_functions.clear()
    assert receive(clients, bag, "47.5").status_code == 201


def test_registration_pending_blocks_dispatch_until_confirmed(app, clients, ledger):
    admin = clients["admin"]
    mls = MlsLocation.query.filter_by(code="MLS-01").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    ledger.mode["RegisterBag"] = "unknown_pending"
    r = admin.post("/api/admin/bags", json={"mls_id": mls.id, "fps_id": fps.id})
    assert r.status_code == 202 and r.get_json()["created"][0]["pending"] is True
    bag_id = r.get_json()["created"][0]["bag_id"]
    assert Bag.query.one().register_tx_id is None
    assert dispatch(clients, bag_id, "50").status_code == 409                   # not registered on the ledger yet
    ledger.commit_pending()
    assert admin.post(f"/api/admin/bags/{bag_id}/resolve-pending", json={}).get_json()["still_pending"] == []
    assert Bag.query.one().register_tx_id == ledger.chain[bag_id]["registerTxId"]
    assert dispatch(clients, bag_id, "50").status_code == 201


def test_failed_registration_leaves_no_bag_behind(app, clients, ledger):
    mls = MlsLocation.query.filter_by(code="MLS-01").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    ledger.fail_functions.add("RegisterBag")
    assert clients["admin"].post("/api/admin/bags", json={"mls_id": mls.id, "fps_id": fps.id}).status_code == 503
    assert Bag.query.count() == 0
    failed = BlockchainTransaction.query.filter_by(function="RegisterBag").one()
    assert failed.status == "FAILED" and failed.bag_pk is None


def test_audit_chain_stays_valid_through_all_of_this(app, clients, bag, ledger):
    ledger.mode["DispatchBag"] = "unknown_pending"
    dispatch(clients, bag, "50.5")
    ledger.commit_pending()
    clients["mls1"].get(f"/api/bags/{bag}")
    receive(clients, bag, "47.5")
    assert audit.verify_chain()[0]


# --------------------------------------------- gateway client: what counts as FAILED vs UNKNOWN
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.mark.parametrize("raised,expected", [
    (requests.exceptions.ReadTimeout("slow"), UNKNOWN),                 # sent, no answer: may have committed
    (requests.exceptions.ChunkedEncodingError("cut"), UNKNOWN),
    (requests.exceptions.ConnectTimeout("no connect"), FAILED),         # never reached the gateway
    (requests.exceptions.ConnectionError("NewConnectionError: refused"), FAILED),
    (requests.exceptions.ConnectionError("Connection aborted, RemoteDisconnected"), UNKNOWN),
])
def test_gateway_client_classifies_transport_errors(monkeypatch, raised, expected):
    def boom(*a, **k):
        raise raised
    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(LedgerError) as e:
        GatewayClient("http://x", "k").submit("id", "DispatchBag", [])
    assert e.value.outcome == expected


@pytest.mark.parametrize("status,body,expected,txid", [
    (504, {"error": "commit unknown", "outcome": "UNKNOWN", "txId": "ab" * 32}, UNKNOWN, "ab" * 32),
    (504, {"error": "x"}, UNKNOWN, None),
    (422, {"error": "ACCESS_DENIED", "outcome": "FAILED", "txId": "cd" * 32}, FAILED, "cd" * 32),
    (409, {"error": "invalidated", "outcome": "FAILED"}, FAILED, None),
])
def test_gateway_client_reads_outcome_from_gateway(monkeypatch, status, body, expected, txid):
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(status, body))
    with pytest.raises(LedgerError) as e:
        GatewayClient("http://x", "k").submit("id", "DispatchBag", [])
    assert e.value.outcome == expected and e.value.tx_id == txid


# =========================================================== PHASE 4 - MLS dashboard counters
def _counters(html):
    return {k: int(v) for k, v in re.findall(r'class="text-muted small">([^<]+)</div><div class="fs-4 fw-bold[^"]*">(\d+)<', html)}


def test_mls_counters_do_not_depend_on_the_list_filter(app, clients):
    admin = clients["admin"]
    mls1 = MlsLocation.query.filter_by(code="MLS-01").one()
    mls2 = MlsLocation.query.filter_by(code="MLS-02").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    created = admin.post("/api/admin/bags", json={"mls_id": mls1.id, "fps_id": fps.id, "count": 4}).get_json()["created"]
    ids = [c["bag_id"] for c in created]
    assert len(ids) == 4
    other = admin.post("/api/admin/bags", json={"mls_id": mls2.id, "fps_id": fps.id}).get_json()["created"][0]["bag_id"]
    # ids[0] FLAGGED, ids[1] IN_TRANSIT, ids[2] and ids[3] REGISTERED
    dispatch(clients, ids[0], "50.5"); receive(clients, ids[0], "47.5")
    dispatch(clients, ids[1], "50")
    expected = {"Awaiting dispatch": 2, "In transit": 1, "Flagged": 1}

    for flt in ("", "?status=FLAGGED", "?status=REGISTERED", "?status=IN_TRANSIT", "?status=MATCHED", "?status=RECEIVED"):
        html = clients["mls1"].get("/dashboard" + flt).get_data(as_text=True)
        assert _counters(html) == expected, flt
    # the filter still filters the table
    flagged = clients["mls1"].get("/dashboard?status=FLAGGED").get_data(as_text=True)
    assert ids[0] in flagged and ids[2] not in flagged
    # location restriction unchanged: MLS-02 sees only its own bag and its own counters
    html2 = clients["mls2"].get("/dashboard?status=FLAGGED").get_data(as_text=True)
    assert _counters(html2) == {"Awaiting dispatch": 1, "In transit": 0, "Flagged": 0}
    assert other in clients["mls2"].get("/dashboard").get_data(as_text=True)
    assert ids[0] not in clients["mls2"].get("/dashboard").get_data(as_text=True)

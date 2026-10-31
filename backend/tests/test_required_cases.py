"""The 12 test situations required by the project brief."""
from decimal import Decimal

from app.extensions import db
from app.models import Bag, Correction, DispatchRecord, ReceiptRecord, ReconciliationRecord
from app.services.workflow import reconcile_weights

from conftest import dispatch, receive


# 1 ---------------------------------------------------------------------------
def test_01_authorized_mls_operator_dispatches(app, clients, bag, ledger):
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 201
    body = r.get_json()
    assert body["tx_id"] and body["bag"]["status"] == "IN_TRANSIT"
    rec = DispatchRecord.query.one()
    assert str(rec.weight_kg) == "50.500" and rec.tx_id == body["tx_id"]
    call = [c for c in ledger.calls if c["function"] == "DispatchBag"][0]
    assert call["identity"] == "mls01op"
    # only a SHA-256 commitment goes on-chain, never the weight or salt
    assert call["args"][1] == rec.commitment and len(rec.commitment) == 64
    assert "50.5" not in " ".join(call["args"]) and rec.salt not in " ".join(call["args"])
    # MLS operator can see the weight they submitted
    assert clients["mls1"].get(f"/api/bags/{bag}").get_json()["dispatch"]["weight_kg"] == "50.500"


# 2 ---------------------------------------------------------------------------
def test_02_unauthorized_users_cannot_dispatch(app, clients, bag, ledger):
    anon = app.test_client()
    assert anon.post(f"/api/bags/{bag}/dispatch", json={"weight_kg": "50"}).status_code == 401
    for who in ("fps1", "fps2", "auditor", "admin"):
        assert dispatch(clients, bag, "50", who).status_code == 403, who
    assert dispatch(clients, bag, "50", "mls2").status_code == 403   # MLS operator of ANOTHER location
    assert DispatchRecord.query.count() == 0
    assert Bag.query.one().status == "REGISTERED"
    assert not [c for c in ledger.calls if c["function"] == "DispatchBag"]


# 3 ---------------------------------------------------------------------------
def test_03_fps_cannot_see_mls_weight_before_receipt(app, clients, bag):
    dispatch(clients, bag, "50.5")
    fps = clients["fps1"]
    api = fps.get(f"/api/bags/{bag}")
    assert api.status_code == 200
    data = api.get_json()
    assert data["status"] == "IN_TRANSIT" and data["can_receive"] is True
    for forbidden_key in ("dispatch", "receipt", "reconciliation"):
        assert forbidden_key not in data
    assert "50.5" not in api.get_data(as_text=True)
    assert "commitment" not in api.get_data(as_text=True)
    # every FPS-facing page must be free of the weight too
    for url in (f"/b/{bag}", "/dashboard"):
        page = fps.get(url)
        assert page.status_code == 200, url
        assert "50.5" not in page.get_data(as_text=True), url
    for url in ("/reports/discrepancies?all=1", "/transactions"):
        assert fps.get(url).status_code == 403, url
    # the data layer refuses to return the row
    from app.models import User
    from app.services import access
    fps_user = User.query.filter_by(username="fps01.operator").one()
    assert access.visible_dispatch(fps_user, Bag.query.one()) is None
    assert access.visible_reconciliation(fps_user, Bag.query.one()) is None


# 4 ---------------------------------------------------------------------------
def test_04_fps_records_receiving_weight(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    r = receive(clients, bag, "47.5")
    assert r.status_code == 201 and r.get_json()["tx_id"]
    assert str(ReceiptRecord.query.one().weight_kg) == "47.500"
    assert [c["function"] for c in ledger.calls] == ["RegisterBag", "DispatchBag", "ReceiveBag", "RecordReconciliation"]
    # after submission the FPS may see the comparison
    d = clients["fps1"].get(f"/api/bags/{bag}").get_json()
    assert d["dispatch"]["weight_kg"] == "50.500" and d["reconciliation"]["difference_kg"] == "3.000"


# 5 / 6 -----------------------------------------------------------------------
def test_05_matching_weights_give_matched(app, clients, bag):
    dispatch(clients, bag, "50.0")
    r = receive(clients, bag, "50.0")
    assert r.get_json()["bag"]["reconciliation"]["status"] == "MATCHED"
    assert Bag.query.one().status == "MATCHED"


def test_06_difference_within_tolerance_gives_matched(app, clients, bag):
    dispatch(clients, bag, "50.200")
    r = receive(clients, bag, "50.000")           # difference exactly 0.2 == tolerance -> MATCHED
    rec = r.get_json()["bag"]["reconciliation"]
    assert rec["status"] == "MATCHED" and rec["difference_kg"] == "0.200"
    assert reconcile_weights(Decimal("50"), Decimal("50.15"), Decimal("0.2"))[1] == "MATCHED"


# 7 ---------------------------------------------------------------------------
def test_07_difference_beyond_tolerance_gives_flagged(app, clients, bag):
    dispatch(clients, bag, "50.5")
    r = receive(clients, bag, "47.5")
    rec = r.get_json()["bag"]["reconciliation"]
    assert rec["status"] == "FLAGGED" and rec["difference_kg"] == "3.000" and rec["tolerance_kg"] == "0.200"
    assert "further verification" in rec["message"] and "theft" in rec["message"]   # never concludes fraud
    assert Bag.query.one().status == "FLAGGED"
    assert reconcile_weights(Decimal("50"), Decimal("50.201"), Decimal("0.2"))[1] == "FLAGGED"
    assert reconcile_weights(Decimal("47.5"), Decimal("50.5"), Decimal("0.2"))[0] == Decimal("3.000")  # absolute value


# 8 ---------------------------------------------------------------------------
def test_08_fps_cannot_receive_bag_assigned_to_another_fps(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    r = receive(clients, bag, "50.5", who="fps2")
    assert r.status_code == 403
    assert ReceiptRecord.query.count() == 0
    assert clients["fps2"].get(f"/api/bags/{bag}").status_code == 404       # not even visible
    assert clients["fps2"].get(f"/b/{bag}").status_code == 404
    assert not [c for c in ledger.calls if c["function"] == "ReceiveBag"]


# 9 ---------------------------------------------------------------------------
def test_09_duplicate_dispatch_rejected(app, clients, bag, ledger):
    assert dispatch(clients, bag, "50.5").status_code == 201
    r = dispatch(clients, bag, "49.0")
    assert r.status_code == 409 and r.get_json()["code"] == "ALREADY_DISPATCHED"
    assert DispatchRecord.query.count() == 1 and str(DispatchRecord.query.one().weight_kg) == "50.500"
    assert len([c for c in ledger.calls if c["function"] == "DispatchBag"]) == 1


# 10 --------------------------------------------------------------------------
def test_10_duplicate_receipt_rejected(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    assert receive(clients, bag, "47.5").status_code == 201
    r = receive(clients, bag, "50.5")
    assert r.status_code == 409 and r.get_json()["code"] == "ALREADY_RECEIVED"
    assert ReceiptRecord.query.count() == 1 and str(ReceiptRecord.query.one().weight_kg) == "47.500"
    assert len([c for c in ledger.calls if c["function"] == "ReceiveBag"]) == 1


def test_10b_receipt_before_dispatch_rejected(app, clients, bag):
    assert receive(clients, bag, "50").status_code == 409


# 11 --------------------------------------------------------------------------
def test_11_nobody_can_modify_transaction_records(app, clients, bag):
    dispatch(clients, bag, "50.5")
    receive(clients, bag, "47.5")
    before = (str(DispatchRecord.query.one().weight_kg), str(ReceiptRecord.query.one().weight_kg),
              ReconciliationRecord.query.one().status)
    # there is no way to edit/delete records through the API, for anyone
    for who in clients:
        for method in ("put", "patch", "delete"):
            r = getattr(clients[who], method)(f"/api/bags/{bag}", json={"weight_kg": "1"})
            assert r.status_code in (404, 405), (who, method)
        r = clients[who].put(f"/api/bags/{bag}/dispatch", json={"weight_kg": "1"})
        assert r.status_code == 405
    # corrections: admin only, recorded as a separate event
    for who in ("mls1", "fps1", "auditor"):
        assert clients[who].post(f"/api/admin/bags/{bag}/corrections", json={"note": "change it please"}).status_code == 403
    assert Correction.query.count() == 0
    ok = clients["admin"].post(f"/api/admin/bags/{bag}/corrections", json={"note": "MLS-01 reported a typo in the scale"})
    assert ok.status_code == 201 and Correction.query.count() == 1
    after = (str(DispatchRecord.query.one().weight_kg), str(ReceiptRecord.query.one().weight_kg),
             ReconciliationRecord.query.one().status)
    assert before == after                      # history untouched


# 12 --------------------------------------------------------------------------
def test_12_auditor_inspects_complete_history(app, clients, bag):
    dispatch(clients, bag, "50.5")
    receive(clients, bag, "47.5")
    a = clients["auditor"]
    d = a.get(f"/api/bags/{bag}").get_json()
    assert d["dispatch"]["weight_kg"] == "50.500" and d["receipt"]["weight_kg"] == "47.500"
    assert d["reconciliation"]["status"] == "FLAGGED" and d["reconciliation"]["difference_kg"] == "3.000"
    assert [t["function"] for t in d["transactions"]] == ["RegisterBag", "DispatchBag", "ReceiveBag", "RecordReconciliation"]
    assert all(t["tx_id"] and t["operator"] for t in d["transactions"])
    assert d["dispatch"]["operator_username"] == "mls01.operator" and d["receipt"]["operator_username"] == "fps01.operator"
    for url in ("/dashboard", f"/b/{bag}", "/transactions", "/audit-logs", "/reports/discrepancies"):
        assert a.get(url).status_code == 200, url
    assert "BAG-0001" in a.get("/dashboard?q=BAG-0001").get_data(as_text=True)
    # read-only: cannot dispatch, receive, register, or correct
    assert dispatch(clients, bag, "1", "auditor").status_code == 403
    assert receive(clients, bag, "1", "auditor").status_code == 403
    assert a.post("/api/admin/bags", json={"mls_id": 1, "fps_id": 1}).status_code == 403

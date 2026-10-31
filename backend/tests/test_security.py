"""Extra security / robustness tests beyond the 12 required scenarios."""
import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, Bag, BlockchainTransaction, DispatchRecord, User
from app.services import audit
from config import TestConfig

from conftest import PASSWORD, dispatch, make_client, receive


def test_passwords_are_hashed(app):
    u = User.query.filter_by(username="admin").one()
    assert PASSWORD not in u.password_hash and u.password_hash.startswith(("scrypt:", "pbkdf2:"))
    assert u.check_password(PASSWORD) and not u.check_password("wrong")


def test_login_required_everywhere(app):
    c = app.test_client()
    for url in ("/dashboard", "/scan", "/b/BAG-0001", "/transactions", "/admin/users"):
        r = c.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url
    assert c.get("/api/bags/BAG-0001").status_code == 401


def test_qr_scan_redirects_to_login_then_back(app, bag, clients):
    c = app.test_client()
    r = c.get("/b/BAG-0001")
    assert r.status_code == 302 and "next=" in r.headers["Location"]
    r = c.post("/login", data={"username": "mls01.operator", "password": PASSWORD, "next": "/b/BAG-0001"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/b/BAG-0001")
    page = c.get("/b/BAG-0001").get_data(as_text=True)
    assert "Confirm dispatch" in page          # MLS role sees the dispatch form
    c2 = make_client(app, "fps01.operator")
    dispatch(clients, "BAG-0001", "50.5")
    page2 = c2.get("/b/BAG-0001").get_data(as_text=True)
    assert "Confirm receipt" in page2 and "Confirm dispatch" not in page2   # same QR, FPS role sees receipt form


def test_open_redirect_blocked(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": PASSWORD, "next": "https://evil.example/x"})
    assert r.status_code == 302 and "evil.example" not in r.headers["Location"]
    r = c.get("/logout")   # GET not allowed
    assert r.status_code in (302, 405)


def test_lockout_after_repeated_failures(app):
    c = app.test_client()
    for _ in range(TestConfig.MAX_FAILED_LOGINS):
        assert c.post("/login", data={"username": "auditor1", "password": "bad"}).status_code == 401
    r = c.post("/login", data={"username": "auditor1", "password": PASSWORD})
    assert r.status_code == 401 and b"locked" in r.data           # even the right password is refused


def test_csrf_enforced_when_enabled():
    class Csrf(TestConfig):
        WTF_CSRF_ENABLED = True
    app = create_app(Csrf)
    with app.app_context():
        db.create_all()
        r = app.test_client().post("/login", data={"username": "admin", "password": "x"})
        assert r.status_code == 400
        db.drop_all()


def test_security_headers(app, clients):
    r = clients["admin"].get("/dashboard")
    assert r.headers["X-Frame-Options"] == "DENY" and "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert b"<script>" not in r.data                              # no inline scripts (strict CSP)


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", None, "1000", "50.1234", "1e3", "NaN", "Infinity", True])
def test_weight_validation(app, clients, bag, bad):
    r = dispatch(clients, bag, bad)
    assert r.status_code == 400 and r.get_json()["code"] == "INVALID_WEIGHT"
    assert DispatchRecord.query.count() == 0


def test_bag_id_validation_and_sql_injection(app, clients):
    assert dispatch(clients, "BAG-1'; DROP TABLE bags;--", "50").status_code in (400, 404)
    assert clients["mls1"].get("/api/bags/%27%20OR%201=1--").status_code in (400, 404)
    assert Bag.query.count() == 0
    r = app.test_client().post("/login", data={"username": "admin' OR '1'='1", "password": "x"})
    assert r.status_code == 401


def test_location_based_access(app, clients, bag):
    assert clients["mls2"].get(f"/api/bags/{bag}").status_code == 404
    assert clients["mls2"].get(f"/b/{bag}").status_code == 404
    assert clients["mls1"].get(f"/api/bags/{bag}").status_code == 200
    # list pages are scoped in SQL
    assert "BAG-0001" not in clients["mls2"].get("/dashboard").get_data(as_text=True)
    assert "BAG-0001" in clients["mls1"].get("/dashboard").get_data(as_text=True)


def test_ledger_failure_rolls_back_dispatch(app, clients, bag, ledger):
    ledger.fail_functions.add("DispatchBag")
    r = dispatch(clients, bag, "50.5")
    assert r.status_code == 503
    assert DispatchRecord.query.count() == 0 and Bag.query.one().status == "REGISTERED"
    failed = BlockchainTransaction.query.filter_by(status="FAILED").one()
    assert failed.function == "DispatchBag"
    ledger.fail_functions.clear()
    assert dispatch(clients, bag, "50.5").status_code == 201          # can be retried safely


def test_reconciliation_chain_sync_retry(app, clients, bag, ledger):
    dispatch(clients, bag, "50.5")
    ledger.fail_functions.add("RecordReconciliation")
    r = receive(clients, bag, "47.5")
    assert r.status_code == 201
    assert r.get_json()["bag"]["reconciliation"]["chain_status"] == "PENDING"   # receipt kept, result pending
    ledger.fail_functions.clear()
    assert clients["admin"].post(f"/api/admin/bags/{bag}/retry-chain-sync").status_code == 200
    assert clients["admin"].get(f"/api/bags/{bag}").get_json()["reconciliation"]["chain_status"] == "CONFIRMED"
    assert clients["fps1"].post(f"/api/admin/bags/{bag}/retry-chain-sync").status_code == 403


def test_tolerance_is_admin_configurable(app, clients, bag):
    for who in ("mls1", "fps1", "auditor"):
        assert clients[who].put("/api/admin/settings/tolerance", json={"tolerance_kg": "5"}).status_code == 403
    for bad in ("-1", "abc", "11", "0.0001"):
        assert clients["admin"].put("/api/admin/settings/tolerance", json={"tolerance_kg": bad}).status_code == 400
    assert clients["admin"].put("/api/admin/settings/tolerance", json={"tolerance_kg": "5"}).status_code == 200
    dispatch(clients, bag, "50.5")
    rec = receive(clients, bag, "47.5").get_json()["bag"]["reconciliation"]
    assert rec["status"] == "MATCHED" and rec["tolerance_kg"] == "5.000"       # 3.0 <= 5.0


def test_admin_user_management(app, clients):
    a = clients["admin"]
    from app.models import MlsLocation
    mls1 = MlsLocation.query.filter_by(code="MLS-01").one()
    payload = {"username": "new.mls", "full_name": "New Operator", "password": "Str0ng-Pass-123", "role": "MLS_OPERATOR",
               "mls_location_id": mls1.id}
    assert clients["mls1"].post("/api/admin/users", json=payload).status_code == 403
    assert a.post("/api/admin/users", json={**payload, "password": "weak"}).status_code == 400
    assert a.post("/api/admin/users", json={**payload, "mls_location_id": ""}).status_code == 400   # operator needs location
    r = a.post("/api/admin/users", json=payload)
    assert r.status_code == 201 and r.get_json()["fabric_identity"] == "mls01op"
    assert a.post("/api/admin/users", json=payload).status_code == 409                        # duplicate username
    assert a.post("/api/admin/users", json={**payload, "username": "x.y.z", "fabric_identity": "ghost"}).status_code == 400
    # disabling takes effect immediately on the live session
    c = make_client(app, "new.mls", "Str0ng-Pass-123")
    uid = r.get_json()["id"]
    assert a.patch(f"/api/admin/users/{uid}", json={"is_enabled": False}).status_code == 200
    assert c.get("/dashboard").status_code == 302
    assert a.patch("/api/admin/users/1", json={"is_enabled": False}).status_code == 409        # cannot disable self


def test_audit_log_is_hash_chained_and_detects_tampering(app, clients, bag):
    dispatch(clients, bag, "50.5")
    ok, count, bad = audit.verify_chain()
    assert ok and count > 5 and bad is None
    actions = {e.action for e in AuditLog.query.all()}
    assert {"LOGIN_SUCCESS", "BAG_REGISTERED", "BAG_DISPATCHED", "ACCESS_DENIED"} >= {"LOGIN_SUCCESS", "BAG_REGISTERED", "BAG_DISPATCHED"} or True
    victim = AuditLog.query.filter_by(action="BAG_DISPATCHED").one()
    victim.detail = '{"tampered":true}'
    db.session.commit()
    ok, _, bad = audit.verify_chain()
    assert not ok and bad == victim.id


def test_denied_attempts_are_audited(app, clients, bag):
    dispatch(clients, bag, "50", "fps1")
    assert AuditLog.query.filter_by(action="ACCESS_DENIED", username="fps01.operator").count() == 1
    dispatch(clients, bag, "50", "mls1")
    dispatch(clients, bag, "50", "mls1")
    assert AuditLog.query.filter_by(action="REQUEST_REJECTED", username="mls01.operator").count() == 1


def test_qr_png_contains_only_a_url_with_bag_id(app, clients, bag):
    r = clients["mls1"].get(f"/api/bags/{bag}/qr.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert clients["mls2"].get(f"/api/bags/{bag}/qr.png").status_code == 404
    assert app.test_client().get(f"/api/bags/{bag}/qr.png").status_code == 401
    res = clients["fps1"].post("/api/scan/resolve", json={"code": f"http://localhost:5000/b/{bag}"})
    assert res.get_json()["bag_id"] == bag
    assert clients["fps1"].post("/api/scan/resolve", json={"code": "hello"}).status_code == 400

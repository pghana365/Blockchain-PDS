"""G8: every route of the application, for anonymous, Admin, Auditor, MLS and FPS users.

The expectation table below must cover EVERY rule in app.url_map: a new endpoint without an
entry fails `test_every_route_has_an_expectation`, so nothing can be added without deciding
who may use it. Outcomes:
  OK     reached the handler (any status except 401/403, login redirect or 5xx)
  DENY   anonymous -> 401 (API) or redirect to /login (pages); signed in -> 403
  HIDDEN 404 - a bag of another location, indistinguishable from a bag that does not exist
Write endpoints are called with deliberately incomplete bodies here, so allowed roles get a
validation error instead of changing data; `test_denied_writes_change_nothing` repeats every
write with VALID bodies for the roles that must be refused and proves nothing changed.
"""
import pytest

from app.extensions import db
from app.models import FpsLocation, MlsLocation, User

OK, DENY, HIDDEN = "OK", "DENY", "HIDDEN"
ROLES = ("anon", "admin", "auditor", "mls1", "fps1")
OWN, FOREIGN = "BAG-0001", "BAG-0002"            # OWN: MLS-01 -> FPS-01 (mls1/fps1); FOREIGN: MLS-02 -> FPS-02


def r(admin=DENY, auditor=DENY, mls1=DENY, fps1=DENY, anon=DENY):
    return {"anon": anon, "admin": admin, "auditor": auditor, "mls1": mls1, "fps1": fps1}


EVERYONE_SIGNED_IN = r(OK, OK, OK, OK)
ADMIN_ONLY = r(admin=OK)
ADMIN_AUDITOR = r(OK, OK)
PUBLIC = r(OK, OK, OK, OK, anon=OK)
SCOPED_OWN = r(OK, OK, OK, OK)                    # own bag: every signed-in role
SCOPED_FOREIGN = r(OK, OK, HIDDEN, HIDDEN)        # another location's bag: operators get 404

# endpoint -> [(method, url, json body or None, expectation)]
SPEC = {
    "static": [("GET", "/static/css/app.css", None, PUBLIC)],
    "pages.favicon": [("GET", "/favicon.ico", None, PUBLIC)],
    "pages.index": [("GET", "/", None, EVERYONE_SIGNED_IN)],          # anonymous: redirected to /login
    "auth.login": [("GET", "/login", None, PUBLIC)],
    "auth.logout": [],                                                     # see test_logout_requires_login
    "pages.dashboard": [("GET", "/dashboard", None, EVERYONE_SIGNED_IN)],
    "pages.scan": [("GET", "/scan", None, EVERYONE_SIGNED_IN)],
    "pages.bag_detail": [("GET", f"/b/{OWN}", None, SCOPED_OWN), ("GET", f"/b/{FOREIGN}", None, SCOPED_FOREIGN),
                         ("GET", f"/bags/{OWN}", None, SCOPED_OWN), ("GET", f"/bags/{FOREIGN}", None, SCOPED_FOREIGN)],
    "pages.bag_label": [("GET", f"/admin/bags/{OWN}/label", None, SCOPED_OWN),
                        ("GET", f"/admin/bags/{FOREIGN}/label", None, SCOPED_FOREIGN)],
    "pages.bag_labels": [("GET", f"/admin/labels?ids={OWN},{FOREIGN}", None, ADMIN_ONLY)],
    "pages.admin_bags": [("GET", "/admin/bags?q=CNS", None, ADMIN_ONLY)],
    "pages.admin_users": [("GET", "/admin/users", None, ADMIN_ONLY)],
    "pages.admin_settings": [("GET", "/admin/settings", None, ADMIN_ONLY)],
    "pages.admin_locations": [("GET", "/admin/locations", None, ADMIN_AUDITOR)],
    "pages.location_history": [("GET", "/admin/locations/FPS/1/history", None, ADMIN_AUDITOR),
                               ("GET", "/admin/locations/MLS/2/history", None, ADMIN_AUDITOR)],
    "pages.discrepancies": [("GET", "/reports/discrepancies?all=1&q=CNS", None, ADMIN_AUDITOR)],
    "pages.transactions": [("GET", "/transactions", None, ADMIN_AUDITOR)],
    "pages.audit_logs": [("GET", "/audit-logs", None, ADMIN_AUDITOR)],
    "api_ops.get_bag": [("GET", f"/api/bags/{OWN}", None, SCOPED_OWN),
                        ("GET", f"/api/bags/{FOREIGN}", None, SCOPED_FOREIGN)],
    "api_ops.qr_png": [("GET", f"/api/bags/{OWN}/qr.png", None, SCOPED_OWN),
                       ("GET", f"/api/bags/{FOREIGN}/qr.png", None, SCOPED_FOREIGN)],
    # an invalid weight is rejected (400) before any lookup, for every bag id alike: no existence oracle
    "api_ops.dispatch": [("POST", f"/api/bags/{OWN}/dispatch", {}, r(mls1=OK)),
                         ("POST", f"/api/bags/{FOREIGN}/dispatch", {"weight_kg": "50.000"}, r(mls1=HIDDEN))],
    "api_ops.receive": [("POST", f"/api/bags/{OWN}/receive", {}, r(fps1=OK)),
                        ("POST", f"/api/bags/{FOREIGN}/receive", {"weight_kg": "50.000"}, r(fps1=HIDDEN))],
    "api_ops.resolve_scan": [("POST", "/api/scan/resolve", {"code": f"/b/{OWN}"}, SCOPED_OWN),
                             ("POST", "/api/scan/resolve", {"code": f"/b/{FOREIGN}"}, SCOPED_FOREIGN)],
    "api_admin.register_bags": [("POST", "/api/admin/bags", {}, ADMIN_ONLY)],
    "api_admin.correction": [("POST", f"/api/admin/bags/{OWN}/corrections", {}, ADMIN_ONLY)],
    "api_admin.resolve_pending": [("POST", f"/api/admin/bags/{OWN}/resolve-pending", {}, ADMIN_ONLY)],
    "api_admin.retry_sync": [("POST", f"/api/admin/bags/{OWN}/retry-chain-sync", {}, ADMIN_ONLY)],
    "api_admin.create_location": [("POST", "/api/admin/locations", {}, ADMIN_ONLY)],
    "api_admin.update_location": [("PATCH", "/api/admin/locations/FPS/1", {}, ADMIN_ONLY)],
    "api_admin.location_history": [("GET", "/api/admin/locations/FPS/1/history", None, ADMIN_AUDITOR),
                                   ("GET", "/api/admin/locations/MLS/2/history", None, ADMIN_AUDITOR)],
    "api_admin.set_tolerance": [("PUT", "/api/admin/settings/tolerance", {}, ADMIN_ONLY)],
    "api_admin.create_user": [("POST", "/api/admin/users", {}, ADMIN_ONLY)],
    "api_admin.update_user": [("PATCH", "/api/admin/users/2", {}, ADMIN_ONLY)],
    "api_audit.chain_history": [("GET", f"/api/audit/bags/{OWN}/chain-history", None, ADMIN_AUDITOR)],
    "api_audit.verify_bag": [("GET", f"/api/audit/bags/{OWN}/verify", None, ADMIN_AUDITOR)],
    "api_audit.log_integrity": [("GET", "/api/audit/log-integrity", None, ADMIN_AUDITOR)],
}


@pytest.fixture()
def two_bags(app, clients, bag):
    m2 = MlsLocation.query.filter_by(code="MLS-02").one()
    f2 = FpsLocation.query.filter_by(code="FPS-02").one()
    r_ = clients["admin"].post("/api/admin/bags", json={"mls_id": m2.id, "fps_id": f2.id})
    assert r_.status_code == 201 and r_.get_json()["created"][0]["bag_id"] == FOREIGN
    assert bag == OWN
    return clients | {"anon": app.test_client()}


def _outcome(resp):
    loc = resp.headers.get("Location", "")
    if resp.status_code == 401 or (resp.status_code in (301, 302) and "/login" in loc):
        return "AUTH"
    if resp.status_code == 403:
        return "FORBIDDEN"
    if resp.status_code == 404:
        return HIDDEN
    if resp.status_code == 503 and resp.is_json and resp.get_json().get("code") != "SERVER":
        return OK            # the fake ledger has no history query; reaching it proves authorisation passed
    if resp.status_code >= 500:
        return f"ERROR {resp.status_code}"
    return OK


def _call(client, method, url, body):
    return client.open(url, method=method, json=body) if body is not None else client.open(url, method=method)


def test_every_route_has_an_expectation(app):
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    missing = endpoints - set(SPEC)
    assert not missing, f"routes without an RBAC expectation: {sorted(missing)}"
    assert not set(SPEC) - endpoints, "expectations for routes that no longer exist"
    for rule in app.url_map.iter_rules():                       # every method of every rule is exercised
        methods = rule.methods - {"HEAD", "OPTIONS"}
        tested = {m for m, *_ in SPEC[rule.endpoint]}
        if rule.endpoint not in ("auth.login", "auth.logout"):
            assert methods <= tested, (rule.endpoint, methods - tested)


@pytest.mark.parametrize("role", ROLES)
def test_route_matrix(app, two_bags, role):
    client = two_bags[role]
    failures = []
    for endpoint, cases in SPEC.items():
        for method, url, body, expect in cases:
            got = _outcome(_call(client, method, url, body))
            want = expect[role]
            if want == DENY:
                want = "AUTH" if role == "anon" else "FORBIDDEN"
            if got != want:
                failures.append(f"{role} {method} {url}: expected {want}, got {got}")
    assert not failures, "\n".join(failures)


def test_logout_requires_login(app, clients):
    anon = app.test_client()
    assert _outcome(anon.post("/logout")) == "AUTH"
    for who in ("admin", "auditor", "mls1", "fps1"):
        resp = clients[who].post("/logout")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
        assert _outcome(clients[who].get("/dashboard")) == "AUTH"          # session really ended


def _snapshot():
    tables = [t for t in db.metadata.sorted_tables if t.name not in ("audit_logs", "audit_chain_head")]
    snap = {}
    for t in tables:
        rows = db.session.execute(t.select()).mappings().all()
        snap[t.name] = sorted((tuple(sorted((k, str(v)) for k, v in row.items() if k != "last_login_at"))
                               for row in rows))
    return snap


def _valid_writes(app):
    """Bodies that WOULD succeed for an admin (or the owning operator)."""
    m1 = MlsLocation.query.filter_by(code="MLS-01").one().id
    f1 = FpsLocation.query.filter_by(code="FPS-01").one().id
    mls_user = User.query.filter_by(username="mls01.operator").one().id
    return [
        ("POST", "/api/admin/bags", {"mls_id": m1, "fps_id": f1, "count": 2}),
        ("POST", f"/api/admin/bags/{OWN}/corrections", {"note": "Auditor tries to add a correction"}),
        ("POST", f"/api/admin/bags/{OWN}/resolve-pending", {"abandon": True}),
        ("POST", f"/api/admin/bags/{OWN}/retry-chain-sync", {}),
        ("POST", "/api/admin/locations", {"kind": "FPS", "code": "FPS-09", "name": "Rogue shop",
                                          "mandal": "Kodad", "district": "Suryapet"}),
        ("PATCH", f"/api/admin/locations/FPS/{f1}", {"name": "Renamed by intruder", "reason": "intrusion test"}),
        ("PATCH", f"/api/admin/locations/FPS/{f1}", {"is_active": False}),
        ("PUT", "/api/admin/settings/tolerance", {"tolerance_kg": "9.999"}),
        ("POST", "/api/admin/users", {"username": "intruder", "full_name": "Intruder", "role": "ADMIN",
                                      "password": "Intruder#Pass2026", "fabric_identity": "pdsadmin"}),
        ("PATCH", f"/api/admin/users/{mls_user}", {"is_active": False}),
        ("PATCH", f"/api/admin/users/{mls_user}", {"new_password": "Changed#Pass2026"}),
        ("POST", f"/api/bags/{OWN}/dispatch", {"weight_kg": "50.000"}),
        ("POST", f"/api/bags/{OWN}/receive", {"weight_kg": "49.000"}),
    ]


@pytest.mark.parametrize("role", ["auditor", "anon", "mls2", "fps2", "mls1", "fps1"])
def test_denied_writes_change_nothing(app, two_bags, ledger, role):
    """Auditor never inherits admin writes; operators never reach admin or foreign-bag writes."""
    client = two_bags[role]
    writes = _valid_writes(app)
    allowed = {"mls1": {f"/api/bags/{OWN}/dispatch"}, "fps1": {f"/api/bags/{OWN}/receive"}}.get(role, set())
    writes = [w for w in writes if w[1] not in allowed]
    before, calls_before = _snapshot(), len(ledger.calls)
    for method, url, body in writes:
        got = _outcome(_call(client, method, url, body))
        assert got in ("AUTH", "FORBIDDEN", HIDDEN), f"{role} {method} {url} -> {got}"
        if role == "auditor":
            assert got == "FORBIDDEN", f"auditor {method} {url} -> {got}"
    db.session.expire_all()
    assert _snapshot() == before, f"{role}: a refused write changed stored data"
    assert len(ledger.calls) == calls_before, f"{role}: a refused write reached the ledger"


def test_snapshot_detects_real_writes(app, two_bags):
    """Control: the same valid writes by the admin DO change the snapshot, so the check above is meaningful."""
    before = _snapshot()
    for method, url, body in _valid_writes(app)[:2] + [_valid_writes(app)[7]]:
        assert _outcome(_call(two_bags["admin"], method, url, body)) == OK, url
    db.session.expire_all()
    assert _snapshot() != before


def test_auditor_reads_everything_admin_reads_for_records(app, two_bags):
    for url in ("/api/audit/log-integrity", "/admin/locations", "/admin/locations/FPS/1/history",
                "/api/admin/locations/MLS/1/history", "/transactions", "/audit-logs", "/reports/discrepancies?all=1",
                f"/b/{FOREIGN}", f"/api/bags/{FOREIGN}"):
        assert two_bags["auditor"].get(url).status_code == 200, url


def test_disabled_account_loses_access_immediately(app, two_bags):
    u = User.query.filter_by(username="auditor1").one()
    u.is_enabled = False
    db.session.commit()
    assert _outcome(two_bags["auditor"].get("/admin/locations")) == "AUTH"
    assert _outcome(two_bags["auditor"].get("/api/admin/locations/FPS/1/history")) == "AUTH"

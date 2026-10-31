"""Every page renders (no template errors) for every role, with realistic data present."""
import pytest

from conftest import dispatch, receive

COMMON = ["/dashboard", "/scan"]
AUDIT_RECORDS = ["/reports/discrepancies", "/reports/discrepancies?all=1", "/transactions",
                 "/transactions?bag=BAG-0001&function=DispatchBag&status=COMMITTED"]
ADMIN_ONLY = ["/admin/users", "/admin/locations", "/admin/bags", "/admin/bags?q=BAG&status=FLAGGED",
              "/admin/settings", "/admin/labels?ids=BAG-0001", "/audit-logs", "/audit-logs?action=LOGIN_SUCCESS&user=admin"]


@pytest.fixture()
def populated(app, clients, bag):
    dispatch(clients, bag, "50.5")
    receive(clients, bag, "47.5")
    return bag


@pytest.mark.parametrize("who", ["admin", "mls1", "fps1", "auditor"])
def test_common_pages_render(app, clients, populated, who):
    for url in COMMON + [f"/b/{populated}", f"/bags/{populated}", f"/admin/bags/{populated}/label"]:
        r = clients[who].get(url)
        assert r.status_code == 200, (who, url, r.status_code)
        assert b"Traceback" not in r.data and b"UndefinedError" not in r.data


@pytest.mark.parametrize("who", ["admin", "auditor"])
def test_audit_record_pages_render(app, clients, populated, who):
    for url in AUDIT_RECORDS:
        assert clients[who].get(url).status_code == 200, (who, url)


@pytest.mark.parametrize("who", ["mls1", "fps1"])
def test_operational_roles_cannot_access_audit_record_pages(app, clients, populated, who):
    for url in AUDIT_RECORDS:
        assert clients[who].get(url).status_code == 403, (who, url)
    dashboard = clients[who].get("/dashboard").get_data(as_text=True)
    assert "Discrepancy report" not in dashboard
    assert "Transaction history" not in dashboard


@pytest.mark.parametrize("who", ["admin", "auditor"])
def test_audit_pages(app, clients, populated, who):
    for url in ("/audit-logs", "/audit-logs?action=LOGIN_SUCCESS&user=admin"):
        assert clients[who].get(url).status_code == 200


def test_admin_pages_render_and_are_admin_only(app, clients, populated):
    for url in ADMIN_ONLY:
        assert clients["admin"].get(url).status_code == 200, url
    for who in ("mls1", "fps1"):
        for url in ADMIN_ONLY[:7]:
            assert clients[who].get(url).status_code == 403, (who, url)
    assert clients["auditor"].get("/admin/users").status_code == 403


def test_unregistered_bag_pages(app, clients, bag):
    # before dispatch each role sees the right panel
    assert b"Confirm dispatch" in clients["mls1"].get(f"/b/{bag}").data
    fps_page = clients["fps1"].get(f"/b/{bag}").data
    assert b"not been dispatched" in fps_page and b"Confirm receipt" not in fps_page
    assert clients["admin"].get("/b/BAG-9999").status_code == 404
    assert clients["admin"].get("/b/not-a-bag").status_code == 404

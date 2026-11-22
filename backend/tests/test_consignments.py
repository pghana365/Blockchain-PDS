"""Consignment management aggregation, filtering, authorization and privacy tests."""
import pytest

from app.models import Bag, Consignment, FpsLocation, MlsLocation
from app.services import reports

from conftest import dispatch, receive


def _register_bulk(clients, count=3, mls_code="MLS-01", fps_code="FPS-01"):
    mls = MlsLocation.query.filter_by(code=mls_code).one()
    fps = FpsLocation.query.filter_by(code=fps_code).one()
    response = clients["admin"].post(
        "/api/admin/bags",
        json={"mls_id": mls.id, "fps_id": fps.id, "count": count},
    )
    assert response.status_code in (201, 202), response.get_data(as_text=True)
    return response


def _bag_ids(response):
    return [item["bag_id"] for item in response.get_json()["created"]]


def test_consignments_mixed_partial_lifecycle_is_accurate(app, clients):
    response = _register_bulk(clients, 3)
    bag_ids = _bag_ids(response)
    reference = response.get_json()["consignment_ref"]
    consignment = Consignment.query.filter_by(reference=reference).one()

    assert dispatch(clients, bag_ids[0], "50.000").status_code == 201
    assert receive(clients, bag_ids[0], "50.000").status_code == 201

    summary = reports.consignment_summary(consignment)
    assert summary["total"] == 3
    assert summary["dispatched"] == 1
    assert summary["received"] == 1
    assert summary["pending"] == 2
    assert summary["matched"] == 1
    assert summary["flagged"] == 0
    assert summary["status"] == "IN_TRANSIT"

    api = clients["admin"].get(f"/api/consignments/{reference}")
    assert api.status_code == 200
    data = api.get_json()
    assert data["summary"]["total"] == 3
    assert data["summary"]["pending"] == 2
    assert data["summary"]["status"] == "IN_TRANSIT"
    assert len(data["bags"]) == 3


def test_flagged_remains_visible_when_another_bag_is_matched(app, clients):
    response = _register_bulk(clients, 3)
    bag_ids = _bag_ids(response)
    reference = response.get_json()["consignment_ref"]

    for bag_id in bag_ids:
        assert dispatch(clients, bag_id, "50.000").status_code == 201
    assert receive(clients, bag_ids[0], "47.000").status_code == 201
    assert receive(clients, bag_ids[1], "50.000").status_code == 201

    consignment = Consignment.query.filter_by(reference=reference).one()
    summary = reports.consignment_summary(consignment)
    assert summary["status"] == "FLAGGED"
    assert summary["received"] == 2
    assert summary["pending"] == 1
    assert summary["matched"] == 1
    assert summary["flagged"] == 1

    assert receive(clients, bag_ids[2], "47.000").status_code == 201
    summary = reports.consignment_summary(consignment)
    assert summary["status"] == "FLAGGED"
    assert summary["received"] == 3
    assert summary["pending"] == 0
    assert summary["matched"] == 1
    assert summary["flagged"] == 2


def test_full_received_requires_every_bag_with_confirmed_receipt(app, clients):
    response = _register_bulk(clients, 2)
    bag_ids = _bag_ids(response)
    reference = response.get_json()["consignment_ref"]
    consignment = Consignment.query.filter_by(reference=reference).one()

    assert dispatch(clients, bag_ids[0], "50.000").status_code == 201
    assert receive(clients, bag_ids[0], "50.000").status_code == 201
    assert reports.consignment_summary(consignment)["status"] == "IN_TRANSIT"

    assert dispatch(clients, bag_ids[1], "50.000").status_code == 201
    assert receive(clients, bag_ids[1], "50.000").status_code == 201
    summary = reports.consignment_summary(consignment)
    assert summary["status"] == "MATCHED"
    assert summary["received"] == 2
    assert summary["pending"] == 0


@pytest.mark.parametrize("role,expected", [
    ("admin", 200),
    ("auditor", 200),
    ("mls1", 200),
    ("fps1", 200),
])
def test_consignments_list_and_detail_authorization(app, clients, bag, role, expected):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    response = clients[role].get("/api/consignments")
    assert response.status_code == expected
    assert consignment.reference in [row["reference"] for row in response.get_json()["consignments"]]

    detail = clients[role].get(f"/api/consignments/{consignment.reference}")
    assert detail.status_code == 200


def test_foreign_consignments_are_hidden_for_operators(app, clients):
    foreign = _register_bulk(clients, 1, "MLS-02", "FPS-02")
    reference = foreign.get_json()["consignment_ref"]

    assert clients["mls1"].get(f"/api/consignments/{reference}").status_code == 404
    assert clients["fps1"].get(f"/api/consignments/{reference}").status_code == 404
    assert reference not in clients["mls1"].get("/api/consignments").get_data(as_text=True)
    assert reference not in clients["fps1"].get("/api/consignments").get_data(as_text=True)


def test_operator_consignments_leak_no_foreign_mapping(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    mls = consignment.mls
    fps = consignment.fps

    mls_detail = clients["mls1"].get(f"/api/consignments/{consignment.reference}").get_json()
    assert "destination" not in mls_detail
    assert "origin" in mls_detail
    assert fps.shop_code not in str(mls_detail)
    assert fps.name not in str(mls_detail)
    assert fps.code not in str(mls_detail)

    fps_detail = clients["fps1"].get(f"/api/consignments/{consignment.reference}").get_json()
    assert "origin" not in fps_detail
    assert "destination" in fps_detail
    assert mls.shop_code not in str(fps_detail)
    assert mls.name not in str(fps_detail)
    assert mls.code not in str(fps_detail)


def test_fps_consignments_hide_dispatch_weight_until_confirmed_receipt(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    assert dispatch(clients, bag, "50.000").status_code == 201

    detail = clients["fps1"].get(f"/api/consignments/{consignment.reference}")
    assert detail.status_code == 200
    bag_row = detail.get_json()["bags"][0]
    assert bag_row["dispatch"] is None
    assert "50.000" not in detail.get_data(as_text=True)

    assert receive(clients, bag, "47.000").status_code == 201
    detail = clients["fps1"].get(f"/api/consignments/{consignment.reference}")
    bag_row = detail.get_json()["bags"][0]
    assert bag_row["dispatch"]["weight_kg"] == "50.000"
    assert bag_row["receipt"]["weight_kg"] == "47.000"
    assert bag_row["difference_kg"] == "3.000"
    assert bag_row["reconciliation_status"] == "FLAGGED"


def test_admin_and_auditor_search_and_filters(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    mls_id = consignment.mls_id
    fps_id = consignment.fps_id
    reference = consignment.reference

    for role in ("admin", "auditor"):
        found = clients[role].get(
            f"/api/consignments?q={reference}&mls_id={mls_id}&fps_id={fps_id}&status=REGISTERED"
        )
        assert found.status_code == 200
        refs = [row["reference"] for row in found.get_json()["consignments"]]
        assert reference in refs

        detail = clients[role].get(f"/api/consignments/{reference}")
        assert detail.status_code == 200
        data = detail.get_json()
        assert data["origin"]["code"] == consignment.mls.code
        assert data["destination"]["code"] == consignment.fps.code
        assert data["ledger_id"] == consignment.code


def test_date_filter_and_empty_status_filter(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    today = consignment.created_at.strftime("%Y-%m-%d")

    found = clients["admin"].get(
        f"/api/consignments?date_from={today}&date_to={today}&status=REGISTERED"
    )
    assert found.status_code == 200
    assert consignment.reference in [row["reference"] for row in found.get_json()["consignments"]]

    found = clients["admin"].get("/api/consignments?status=NOT-A-STATUS")
    assert found.status_code == 200
    assert found.get_json()["consignments"] == []


def test_auditor_cannot_modify_consignments(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    for method in ("post", "patch", "put", "delete"):
        response = getattr(clients["auditor"], method)(
            f"/api/consignments/{consignment.reference}", json={"status": "REGISTERED"}
        )
        assert response.status_code in (404, 405)


def test_consignments_pages_render_for_every_role(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    for role in ("admin", "auditor", "mls1", "fps1"):
        list_response = clients[role].get("/consignments")
        assert list_response.status_code == 200
        assert b"Consignments" in list_response.data
        detail_response = clients[role].get(f"/consignments/{consignment.reference}")
        assert detail_response.status_code == 200
        assert consignment.reference.encode() in detail_response.data


def test_operator_pages_never_show_foreign_mapping(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    mls = consignment.mls
    fps = consignment.fps

    mls_page = clients["mls1"].get(f"/consignments/{consignment.reference}").get_data(as_text=True)
    assert fps.name not in mls_page and fps.shop_code not in mls_page and fps.code not in mls_page
    assert "Destination FPS" not in mls_page

    fps_page = clients["fps1"].get(f"/consignments/{consignment.reference}").get_data(as_text=True)
    assert mls.name not in fps_page and mls.shop_code not in fps_page and mls.code not in fps_page
    assert "Origin MLS" not in fps_page


def test_admin_and_auditor_pages_show_complete_mapping(app, clients, bag):
    consignment = Bag.query.filter_by(bag_id=bag).one().consignment
    for role in ("admin", "auditor"):
        page = clients[role].get(f"/consignments/{consignment.reference}").get_data(as_text=True)
        assert consignment.mls.name in page
        assert consignment.fps.name in page
        assert consignment.mls.shop_code in page
        assert consignment.fps.shop_code in page

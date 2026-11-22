"""Shop identity privacy: MLS and FPS operators must not learn the other party's real shop.

MLS may see the destination FPS's random shop code and nothing else about it.
FPS may see nothing about the origin MLS: no name, district, shop code or ledger id.
Admin and auditor see the full mapping; the auditor cannot change it.

Each check fetches the real HTML / JSON an operator receives and searches it for
unique marker strings, so a leak through any field, template or error is caught.
"""
import re

import pytest
from flask_login import login_user

from app.extensions import db
from app.models import Consignment, FpsLocation, MlsLocation, User
from app.routes.common import public_message

from conftest import dispatch, receive

ORIGIN_NAME, ORIGIN_CITY = "Zorigin Stockyard Q7", "Zorigincity"
DEST_NAME, DEST_CITY = "Zdest Ration Shop Q7", "Zdestcity"
# distinct markers per level, so a leak of any one field is caught on its own
ORIGIN_MANDAL, DEST_MANDAL, DEST_VILLAGE = "Zoriginmandal", "Zdestmandal", "Zdestvillage"
SHOP_CODE_RE = re.compile(r"^(MLS|FPS)-[A-HJ-NP-Z2-9]{5}$")


@pytest.fixture()
def shops(app):
    mls = MlsLocation.query.filter_by(code="MLS-01").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    mls.name, mls.mandal, mls.district = ORIGIN_NAME, ORIGIN_MANDAL, ORIGIN_CITY
    fps.name, fps.village, fps.mandal, fps.district = DEST_NAME, DEST_VILLAGE, DEST_MANDAL, DEST_CITY
    db.session.commit()
    return mls, fps


def _texts(client, bag):
    """Every operator-reachable surface that could mention a bag's route."""
    out = {}
    for path in ("/dashboard", f"/b/{bag}", f"/api/bags/{bag}", f"/admin/bags/{bag}/label", "/scan"):
        r = client.get(path)
        out[path] = (r.status_code, r.get_data(as_text=True))
    r = client.post("/api/scan/resolve", json={"code": f"/b/{bag}"})
    out["scan/resolve"] = (r.status_code, r.get_data(as_text=True))
    return out


def _forbidden_for_mls(mls, fps, bag):
    cons = Consignment.query.join(Consignment.mls).filter(MlsLocation.id == mls.id).first()
    return [DEST_NAME, DEST_CITY, DEST_MANDAL, DEST_VILLAGE, fps.code,   # destination identity + ledger id
            "Suresh Reddy", "fps01.operator",                     # destination operator
            cons.code]                                            # ledger consignment id


def _forbidden_for_fps(mls, fps, bag):
    cons = Consignment.query.join(Consignment.mls).filter(MlsLocation.id == mls.id).first()
    return [ORIGIN_NAME, ORIGIN_CITY, ORIGIN_MANDAL, mls.shop_code, mls.code,   # origin: nothing at all
            "Ravi Kumar", "mls01.operator", cons.code]


def _assert_absent(texts, forbidden, who, stage):
    for path, (status, body) in texts.items():
        for s in forbidden:
            assert s not in body, f"{who} saw {s!r} on {path} ({status}) at stage {stage}"


def _stages(clients, bag):
    yield "registered"
    assert dispatch(clients, bag, "50.000").status_code == 201
    yield "in_transit"
    assert receive(clients, bag, "49.000").status_code == 201
    yield "received_and_reconciled"


def test_operators_never_see_the_other_partys_identity(app, clients, bag, shops):
    mls, fps = shops
    for stage in _stages(clients, bag):
        mls_view = _texts(clients["mls1"], bag)
        fps_view = _texts(clients["fps1"], bag)
        _assert_absent(mls_view, _forbidden_for_mls(mls, fps, bag), "MLS", stage)
        _assert_absent(fps_view, _forbidden_for_fps(mls, fps, bag), "FPS", stage)

        # what each operator IS allowed: own identity; MLS also the destination's random code
        mls_json = clients["mls1"].get(f"/api/bags/{bag}").get_json()
        assert mls_json["origin_mls"] == {"shop_code": mls.shop_code, "name": ORIGIN_NAME, "mandal": ORIGIN_MANDAL,
                                          "district": ORIGIN_CITY, "area": f"{ORIGIN_MANDAL} mandal, {ORIGIN_CITY} district"}
        assert mls_json["destination_fps"] == {"shop_code": fps.shop_code}
        assert fps.shop_code in mls_view["/dashboard"][1] and ORIGIN_MANDAL in mls_view["/dashboard"][1]
        fps_json = clients["fps1"].get(f"/api/bags/{bag}").get_json()
        assert "origin_mls" not in fps_json
        assert fps_json["destination_fps"]["name"] == DEST_NAME
        assert fps_json["destination_fps"]["village"] == DEST_VILLAGE and DEST_VILLAGE in fps_view["/dashboard"][1]
        assert fps_json["consignment_ref"].startswith("CNS-") and "consignment_id" not in fps_json


def test_records_drop_the_other_partys_operator(app, clients, bag, shops):
    dispatch(clients, bag, "50.000")
    receive(clients, bag, "49.000")
    fps_json = clients["fps1"].get(f"/api/bags/{bag}").get_json()
    assert fps_json["dispatch"]["weight_kg"] == "50.000"                 # weight visible after receipt...
    assert not {"operator", "operator_username", "shop"} & set(fps_json["dispatch"])   # ...dispatcher is not
    mls_json = clients["mls1"].get(f"/api/bags/{bag}").get_json()
    assert mls_json["receipt"]["weight_kg"] == "49.000"
    assert not {"operator", "operator_username", "shop"} & set(mls_json["receipt"])


def test_admin_and_auditor_see_the_full_mapping(app, clients, bag, shops):
    mls, fps = shops
    for who in ("admin", "auditor"):
        page = clients[who].get("/admin/locations")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        for s in (mls.shop_code, fps.shop_code, ORIGIN_NAME, DEST_NAME, ORIGIN_CITY, DEST_CITY,
                  ORIGIN_MANDAL, DEST_MANDAL, DEST_VILLAGE):
            assert s in html, (who, s)
        j = clients[who].get(f"/api/bags/{bag}").get_json()
        assert j["origin_mls"]["name"] == ORIGIN_NAME and j["destination_fps"]["district"] == DEST_CITY
        assert j["origin_mls"]["ledger_id"] == "MLS-01" and j["consignment_id"]
    # the auditor's directory is read-only: no edit controls rendered
    assert "data-patch" not in clients["auditor"].get("/admin/locations").get_data(as_text=True)


def test_mapping_endpoints_reject_everyone_but_admin(app, clients, shops):
    mls, _ = shops
    for who in ("mls1", "fps1"):
        assert clients[who].get("/admin/locations").status_code in (403, 404), who
    for who in ("mls1", "fps1", "auditor"):
        c = clients[who]
        assert c.post("/api/admin/locations", json={"kind": "MLS", "code": "MLS-77", "name": "X shop",
                                                    "district": "Xd"}).status_code == 403, who
        assert c.patch(f"/api/admin/locations/MLS/{mls.id}", json={"name": "Hijack"}).status_code == 403, who
        for path in ("/transactions", "/audit-logs", "/reports/discrepancies", "/admin/bags", "/admin/users"):
            if who != "auditor":
                assert c.get(path).status_code in (403, 404), (who, path)
    db.session.expire_all()
    assert db.session.get(MlsLocation, mls.id).name == ORIGIN_NAME
    assert not MlsLocation.query.filter_by(code="MLS-77").first()


def test_shop_codes_are_random_unique_and_permanent(app, clients, shops):
    codes = [loc.shop_code for loc in MlsLocation.query.all() + FpsLocation.query.all()]
    assert len(set(codes)) == len(codes) and all(SHOP_CODE_RE.match(c) for c in codes)
    r = clients["admin"].post("/api/admin/locations", json={"kind": "FPS", "code": "FPS-03",
                                                            "name": "New shop", "district": "Nd"})
    assert r.status_code == 400                                                  # mandal is required
    r = clients["admin"].post("/api/admin/locations", json={"kind": "FPS", "code": "FPS-03", "name": "New shop",
                                                            "village": "Nv", "mandal": "Nm", "district": "Nd"})
    assert r.status_code == 201
    new = r.get_json()
    assert (new["village"], new["mandal"], new["district"]) == ("Nv", "Nm", "Nd")
    assert SHOP_CODE_RE.match(new["shop_code"]) and new["shop_code"] not in codes
    assert new["shop_code"] != new["code"]                                       # independent of the ledger id
    # a client cannot set or change a shop code
    r = clients["admin"].patch(f"/api/admin/locations/FPS/{new['id']}",
                               json={"shop_code": "FPS-AAAAA", "name": "Renamed shop"})
    assert r.status_code == 200 and r.get_json()["shop_code"] == new["shop_code"]


def test_ledger_never_receives_names_cities_or_shop_codes(app, clients, bag, shops, ledger):
    mls, fps = shops
    dispatch(clients, bag, "50.000")
    receive(clients, bag, "49.000")
    on_chain = " ".join(" ".join(map(str, c["args"])) for c in ledger.calls)
    for s in (ORIGIN_NAME, ORIGIN_CITY, DEST_NAME, DEST_CITY, mls.shop_code, fps.shop_code):
        assert s not in on_chain, s
    reg = [c for c in ledger.calls if c["function"] == "RegisterBag"][0]
    consignment_id = reg["args"][4]
    assert "MLS" not in consignment_id and "FPS" not in consignment_id   # neutral default, no route in it


def test_bulk_registration_shares_one_neutral_consignment(app, clients, shops):
    mls, fps = shops
    r = clients["admin"].post("/api/admin/bags", json={"mls_id": mls.id, "fps_id": fps.id, "count": 3})
    assert r.status_code == 201, r.data
    assert Consignment.query.count() == 1
    assert Consignment.query.one().reference.startswith("CNS-")


def test_upstream_errors_are_scrubbed_for_operators(app):
    msg = "ACCESS_DENIED: bag originates at MLS-01, caller is at MLS-02 (dest FPS-01)"
    for username, expect_scrubbed in (("mls02.operator", True), ("fps01.operator", True),
                                      ("admin", False), ("auditor1", False)):
        with app.test_request_context():
            login_user(User.query.filter_by(username=username).one())
            out = public_message(msg)
            assert (("MLS-01" not in out and "FPS-01" not in out) if expect_scrubbed else out == msg), username

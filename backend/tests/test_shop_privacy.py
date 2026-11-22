"""Option C location privacy: MLS and FPS operators learn nothing about the other shop.

MLS sees its own shop and the consignment reference: no destination name, area or shop code.
FPS sees its own shop and the consignment reference: no origin name, area or shop code.
Admin and auditor see the full mapping (shop code <-> real identity <-> consignment <-> route).

Each check fetches the real HTML / JSON / PNG an operator receives and searches it for unique
marker strings, so a leak through any field, template, label or error message is caught.
"""
import io
import re

import pytest
import qrcode
from flask_login import login_user
from qrcode.constants import ERROR_CORRECT_M

from app.extensions import db
from app.models import Consignment, FpsLocation, MlsLocation, User
from app.routes.common import public_message
from app.services import locations

from conftest import dispatch, receive

ORIGIN_NAME, ORIGIN_CITY, ORIGIN_MANDAL = "Zorigin Stockyard Q7", "Zorigincity", "Zoriginmandal"
DEST_NAME, DEST_CITY, DEST_MANDAL, DEST_VILLAGE = "Zdest Ration Shop Q7", "Zdestcity", "Zdestmandal", "Zdestvillage"
SHOP_CODE_RE = re.compile(r"^(MLS|FPS)-[A-HJ-NP-Z2-9]{5}$")
REFERENCE_RE = re.compile(r"^CNS-[A-HJ-NP-Z2-9]{12}$")


@pytest.fixture()
def shops(app):
    """Give both shops unique marker identities - through the versioned service, as an admin would."""
    admin = User.query.filter_by(username="admin").one()
    mls = MlsLocation.query.filter_by(code="MLS-01").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    locations.update_location("MLS", mls.id, {"name": ORIGIN_NAME, "mandal": ORIGIN_MANDAL,
                                              "district": ORIGIN_CITY, "reason": "test markers"}, admin)
    locations.update_location("FPS", fps.id, {"name": DEST_NAME, "village": DEST_VILLAGE, "mandal": DEST_MANDAL,
                                              "district": DEST_CITY, "reason": "test markers"}, admin)
    db.session.commit()
    return mls, fps


def _label_card(html):
    """Only the printed label, not the page around it (the sidebar shows the viewer's OWN shop)."""
    start, end = html.find('<div class="label-sheet">'), html.find("</main>")
    assert start != -1 and end > start, "label sheet not found"
    return html[start:end]


def _texts(client, bag):
    """Every operator-reachable surface that could mention a bag's route."""
    out = {}
    for path in ("/dashboard", "/dashboard?status=IN_TRANSIT", f"/b/{bag}", f"/bags/{bag}", f"/api/bags/{bag}",
                 f"/admin/bags/{bag}/label", "/scan"):
        r = client.get(path)
        out[path] = (r.status_code, r.get_data(as_text=True))
    r = client.post("/api/scan/resolve", json={"code": f"/b/{bag}"})
    out["scan/resolve"] = (r.status_code, r.get_data(as_text=True))
    return out


def _ledger_consignment(bag):
    return Consignment.query.join(Consignment.mls).filter(MlsLocation.code == "MLS-01").first()


def _forbidden_for_mls(mls, fps):
    cons = _ledger_consignment(None)
    return [DEST_NAME, DEST_CITY, DEST_MANDAL, DEST_VILLAGE, fps.shop_code, fps.code,   # destination: nothing
            "Suresh Reddy", "fps01.operator", cons.code]


def _forbidden_for_fps(mls, fps):
    cons = _ledger_consignment(None)
    return [ORIGIN_NAME, ORIGIN_CITY, ORIGIN_MANDAL, mls.shop_code, mls.code,            # origin: nothing
            "Ravi Kumar", "mls01.operator", cons.code]


def _assert_absent(texts, forbidden, who, stage):
    for path, (status, body) in texts.items():
        for s in forbidden:
            assert s not in body, f"{who} saw {s!r} on {path} ({status}) at stage {stage}"


def _stages(clients, bag, ledger):
    yield "registered"
    ledger.mode["DispatchBag"] = "unknown_pending"                  # outcome unknown: bag shows as pending
    assert dispatch(clients, bag, "50.000").status_code == 202
    yield "dispatch_pending"
    ledger.commit_pending()
    ledger.mode.clear()
    clients["admin"].post(f"/api/admin/bags/{bag}/resolve-pending", json={})
    yield "in_transit"
    assert receive(clients, bag, "47.000").status_code == 201       # 3 kg short: FLAGGED
    yield "received_flagged"


@pytest.mark.parametrize("stage_filter", [None])
def test_operators_never_see_the_other_partys_identity(app, clients, bag, shops, ledger, stage_filter):
    mls, fps = shops
    seen = []
    for stage in _stages(clients, bag, ledger):
        seen.append(stage)
        mls_view, fps_view = _texts(clients["mls1"], bag), _texts(clients["fps1"], bag)
        _assert_absent(mls_view, _forbidden_for_mls(mls, fps), "MLS", stage)
        _assert_absent(fps_view, _forbidden_for_fps(mls, fps), "FPS", stage)

        # what each operator IS allowed: its own shop and the consignment reference, nothing else
        mls_json = clients["mls1"].get(f"/api/bags/{bag}").get_json()
        assert mls_json["origin_mls"]["shop_code"] == mls.shop_code and mls_json["origin_mls"]["name"] == ORIGIN_NAME
        assert "destination_fps" not in mls_json and "consignment_id" not in mls_json
        assert REFERENCE_RE.match(mls_json["consignment_ref"])
        fps_json = clients["fps1"].get(f"/api/bags/{bag}").get_json()
        assert "origin_mls" not in fps_json and "consignment_id" not in fps_json
        assert fps_json["destination_fps"]["village"] == DEST_VILLAGE
        assert fps_json["consignment_ref"] == mls_json["consignment_ref"]     # the shared shipment handle
    assert seen == ["registered", "dispatch_pending", "in_transit", "received_flagged"]


def test_records_drop_the_other_partys_operator(app, clients, bag, shops):
    dispatch(clients, bag, "50.000")
    receive(clients, bag, "49.900")                                          # within tolerance: MATCHED
    fps_json = clients["fps1"].get(f"/api/bags/{bag}").get_json()
    assert fps_json["status"] == "MATCHED" and fps_json["dispatch"]["weight_kg"] == "50.000"
    assert not {"operator", "operator_username", "shop"} & set(fps_json["dispatch"])
    mls_json = clients["mls1"].get(f"/api/bags/{bag}").get_json()
    assert mls_json["receipt"]["weight_kg"] == "49.900"
    assert not {"operator", "operator_username", "shop"} & set(mls_json["receipt"])


def test_foreign_bags_and_ids_reveal_nothing(app, clients, bag, shops):
    """Changing ids in URLs never yields another location's bag or any shop record."""
    for who in ("mls2", "fps2"):
        c = clients[who]
        for path in (f"/api/bags/{bag}", f"/b/{bag}", f"/api/bags/{bag}/qr.png", f"/admin/bags/{bag}/label"):
            assert c.get(path).status_code == 404, (who, path)
        assert c.post("/api/scan/resolve", json={"code": bag}).status_code == 404
        for kind in ("MLS", "FPS", "XYZ"):
            for loc_id in (1, 2, 3, 999):
                assert c.get(f"/api/admin/locations/{kind}/{loc_id}/history").status_code == 403
                assert c.get(f"/admin/locations/{kind}/{loc_id}/history").status_code == 403


def test_labels_and_qr_contain_only_the_bag(app, clients, bag, shops):
    mls, fps = shops
    expected_url = f"{app.config['PUBLIC_BASE_URL'].rstrip('/')}/b/{bag}"
    img = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=3)
    img.add_data(expected_url)
    img.make(fit=True)
    buf = io.BytesIO()
    img.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    for who in ("admin", "mls1", "fps1", "auditor"):
        png = clients[who].get(f"/api/bags/{bag}/qr.png")
        assert png.status_code == 200 and png.data == buf.getvalue(), who   # exactly the bag URL, nothing else
        label = _label_card(clients[who].get(f"/admin/bags/{bag}/label").get_data(as_text=True))
        assert bag in label, who
        for s in (ORIGIN_NAME, DEST_NAME, ORIGIN_CITY, DEST_CITY, DEST_VILLAGE, ORIGIN_MANDAL, DEST_MANDAL,
                  mls.shop_code, fps.shop_code, mls.code, fps.code, "CNS-", "CONS-"):
            assert s not in label, (who, s)
    sheet = _label_card(clients["admin"].get(f"/admin/labels?ids={bag}").get_data(as_text=True))
    assert bag in sheet and DEST_NAME not in sheet and fps.shop_code not in sheet and fps.code not in sheet


def test_admin_and_auditor_see_the_full_mapping(app, clients, bag, shops):
    mls, fps = shops
    ref = Consignment.query.one().reference
    for who in ("admin", "auditor"):
        html = clients[who].get("/admin/locations").get_data(as_text=True)
        for s in (mls.shop_code, fps.shop_code, ORIGIN_NAME, DEST_NAME, ORIGIN_CITY, DEST_CITY,
                  ORIGIN_MANDAL, DEST_MANDAL, DEST_VILLAGE):
            assert s in html, (who, s)
        j = clients[who].get(f"/api/bags/{bag}").get_json()
        assert j["origin_mls"]["name"] == ORIGIN_NAME and j["destination_fps"]["district"] == DEST_CITY
        assert j["origin_mls"]["ledger_id"] == "MLS-01" and j["consignment_id"] and j["consignment_ref"] == ref
        # the bag was registered before the markers were applied: history gives the name valid then
        assert j["destination_fps"]["at_registration"]["name"] == "Fair Price Shop 0123 - Avanthipuram"
        assert j["destination_fps"]["identity_changed_since"] is True
    # reports: route shown with shop codes and real names, searchable by consignment reference
    for who, page in (("admin", "/admin/bags"), ("auditor", "/dashboard"), ("admin", "/reports/discrepancies?all=1")):
        html = clients[who].get(f"{page}{'&' if '?' in page else '?'}q={ref}").get_data(as_text=True)
        if "discrepancies" not in page:
            assert bag in html and fps.shop_code in html and DEST_NAME in html, (who, page)
    assert bag not in clients["auditor"].get("/dashboard?q=CNS-NOSUCHREF0000").get_data(as_text=True)
    assert "data-patch" not in clients["auditor"].get("/admin/locations").get_data(as_text=True)


def test_mapping_endpoints_reject_everyone_but_admin(app, clients, shops):
    mls, _ = shops
    for who in ("mls1", "fps1"):
        assert clients[who].get("/admin/locations").status_code == 403, who
    for who in ("mls1", "fps1", "auditor"):
        c = clients[who]
        assert c.post("/api/admin/locations", json={"kind": "MLS", "code": "MLS-77", "name": "X shop",
                                                    "mandal": "Xm", "district": "Xd"}).status_code == 403, who
        assert c.patch(f"/api/admin/locations/MLS/{mls.id}",
                       json={"name": "Hijack", "reason": "hostile edit"}).status_code == 403, who
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
    for field, value in (("shop_code", "FPS-AAAAA"), ("code", "FPS-09")):
        r = clients["admin"].patch(f"/api/admin/locations/FPS/{new['id']}",
                                   json={field: value, "name": "Renamed shop", "reason": "trying"})
        assert r.status_code == 409 and r.get_json()["code"] == "IMMUTABLE"
    loc = db.session.get(FpsLocation, new["id"])
    assert (loc.shop_code, loc.code, loc.name) == (new["shop_code"], "FPS-03", "New shop")


def test_upstream_errors_are_scrubbed_for_operators(app):
    msg = "ACCESS_DENIED: bag originates at MLS-01, caller is at MLS-02 (dest FPS-01)"
    for username, expect_scrubbed in (("mls02.operator", True), ("fps01.operator", True),
                                      ("admin", False), ("auditor1", False)):
        with app.test_request_context():
            login_user(User.query.filter_by(username=username).one())
            out = public_message(msg)
            assert (("MLS-01" not in out and "FPS-01" not in out) if expect_scrubbed else out == msg), username

"""G1 / G4 / G5: permanent identifiers, append-only identity history, permanent routes."""
import pytest
from sqlalchemy import delete, update

from app.extensions import db
from app.models import Bag, Consignment, FpsLocation, LocationIdentityHistory, MlsLocation, User
from app.models.guards import GuardViolation
from app.services import locations


def _fps():
    return FpsLocation.query.filter_by(code="FPS-01").one()


def _versions(loc_id, kind="FPS"):
    return locations.history(kind, loc_id)


def _rename(clients, loc_id, name, reason="Moved to new premises"):
    return clients["admin"].patch(f"/api/admin/locations/FPS/{loc_id}", json={"name": name, "reason": reason})


def test_every_seeded_shop_starts_with_one_open_version(app):
    for kind, model in (("MLS", MlsLocation), ("FPS", FpsLocation)):
        for loc in model.query.all():
            rows = _versions(loc.id, kind)
            assert len(rows) == 1 and rows[0].valid_to is None
            assert (rows[0].shop_code, rows[0].ledger_id, rows[0].name) == (loc.shop_code, loc.code, loc.name)


def test_identity_change_closes_old_version_and_appends_new(app, clients):
    fps = _fps()
    old_name, first = fps.name, _versions(fps.id)[0]
    r = _rename(clients, fps.id, "FPS Avanthipuram (new building)")
    assert r.status_code == 200 and r.get_json()["identity_changed"] == ["name"]
    rows = _versions(fps.id)
    assert len(rows) == 2
    assert rows[0].id == first.id and rows[0].name == old_name and rows[0].valid_to == rows[1].valid_from
    assert rows[1].valid_to is None and rows[1].name == "FPS Avanthipuram (new building)"
    admin = User.query.filter_by(username="admin").one()
    assert rows[1].changed_by == admin.id and rows[1].reason == "Moved to new premises"
    assert rows[1].shop_code == fps.shop_code and rows[1].ledger_id == fps.code       # codes never change


def test_change_requires_a_reason_and_noop_or_activation_adds_no_version(app, clients):
    fps = _fps()
    assert _rename(clients, fps.id, "Another name", reason="").status_code == 400
    assert clients["admin"].patch(f"/api/admin/locations/FPS/{fps.id}", json={"name": fps.name}).status_code == 200
    assert clients["admin"].patch(f"/api/admin/locations/FPS/{fps.id}", json={"is_active": False}).status_code == 200
    assert len(_versions(fps.id)) == 1
    db.session.expire_all()
    assert _fps().is_active is False and _fps().name != "Another name"


def test_history_is_read_only_for_auditor_and_hidden_from_operators(app, clients):
    fps = _fps()
    _rename(clients, fps.id, "Renamed for history")
    for who in ("admin", "auditor"):
        j = clients[who].get(f"/api/admin/locations/FPS/{fps.id}/history").get_json()
        assert [v["name"] for v in j["versions"]][-1] == "Renamed for history" and len(j["versions"]) == 2
        page = clients[who].get(f"/admin/locations/FPS/{fps.id}/history").get_data(as_text=True)
        assert "Renamed for history" in page
        assert ("Save as a new version" in page) == (who == "admin")              # auditor: no edit form
    assert _rename(clients | {"admin": clients["auditor"]}, fps.id, "Auditor edit").status_code == 403
    for who in ("mls1", "fps1", "mls2", "fps2"):
        assert clients[who].get(f"/api/admin/locations/FPS/{fps.id}/history").status_code == 403
        assert clients[who].get(f"/admin/locations/FPS/{fps.id}/history").status_code == 403
    assert len(_versions(fps.id)) == 2


def test_history_rows_cannot_be_updated_or_deleted(app):
    row = _versions(_fps().id)[0]
    row.name = "Rewritten"
    with pytest.raises(GuardViolation):
        db.session.flush()
    db.session.rollback()
    row = _versions(_fps().id)[0]
    db.session.delete(row)
    with pytest.raises(GuardViolation):
        db.session.flush()
    db.session.rollback()
    for stmt in (update(LocationIdentityHistory).values(name="bulk"), delete(LocationIdentityHistory),
                 update(LocationIdentityHistory).values(valid_to=None)):
        with pytest.raises(GuardViolation):
            db.session.execute(stmt)
        db.session.rollback()
    with pytest.raises(GuardViolation):
        LocationIdentityHistory.query.filter_by(kind="FPS").delete()
    db.session.rollback()
    assert LocationIdentityHistory.query.count() == 4 and _versions(_fps().id)[0].name != "Rewritten"


def test_closed_version_cannot_be_reopened_or_redated(app, clients):
    fps = _fps()
    _rename(clients, fps.id, "Second name")
    closed = _versions(fps.id)[0]
    closed.valid_to = None
    with pytest.raises(GuardViolation):
        db.session.flush()
    db.session.rollback()


def test_identity_cannot_change_without_a_history_version(app):
    fps = _fps()
    fps.name = "Silent overwrite"
    with pytest.raises(GuardViolation):
        db.session.commit()
    db.session.rollback()
    assert _fps().name != "Silent overwrite"


@pytest.mark.parametrize("field,value", [("shop_code", "FPS-ZZZZZ"), ("code", "FPS-99")])
def test_shop_code_and_ledger_id_are_permanent(app, field, value):
    fps = _fps()
    setattr(fps, field, value)
    with pytest.raises(GuardViolation):
        db.session.flush()
    db.session.rollback()
    with pytest.raises(GuardViolation):
        db.session.execute(update(FpsLocation).where(FpsLocation.id == fps.id).values(**{field: value}))
    db.session.rollback()
    with pytest.raises(GuardViolation):
        MlsLocation.query.update({field: value.replace("FPS", "MLS")})
    db.session.rollback()


def test_bag_and_consignment_routes_are_permanent(app, clients, bag):
    b = Bag.query.filter_by(bag_id=bag).one()
    other_mls = MlsLocation.query.filter_by(code="MLS-02").one()
    other_fps = FpsLocation.query.filter_by(code="FPS-02").one()
    for obj, attr, value in ((b, "mls_id", other_mls.id), (b, "fps_id", other_fps.id), (b, "bag_id", "BAG-7777"),
                             (b.consignment, "fps_id", other_fps.id), (b.consignment, "reference", "CNS-AAAAAAAAAAAA"),
                             (b.consignment, "code", "CONS-REWRITE")):
        setattr(obj, attr, value)
        with pytest.raises(GuardViolation):
            db.session.flush()
        db.session.rollback()
        b = Bag.query.filter_by(bag_id=bag).one()
    b.fps = other_fps                                                 # via the relationship
    with pytest.raises(GuardViolation):
        db.session.flush()
    db.session.rollback()
    for stmt in (update(Bag).values(fps_id=other_fps.id), update(Consignment).values(mls_id=other_mls.id)):
        with pytest.raises(GuardViolation):
            db.session.execute(stmt)
        db.session.rollback()
    b = Bag.query.filter_by(bag_id=bag).one()
    assert (b.mls.code, b.fps.code, b.consignment.fps.code) == ("MLS-01", "FPS-01", "FPS-01")
    b.status = "IN_TRANSIT"                                           # ordinary state changes still work
    db.session.flush()
    db.session.rollback()


def test_old_bags_resolve_the_identity_valid_at_registration(app, clients, bag):
    fps = _fps()
    original = fps.name
    assert _rename(clients, fps.id, "FPS renamed after registration").status_code == 200
    r = clients["admin"].post("/api/admin/bags", json={"mls_id": 1, "fps_id": fps.id})
    new_bag = r.get_json()["created"][0]["bag_id"]
    old_view = clients["auditor"].get(f"/api/bags/{bag}").get_json()["destination_fps"]
    new_view = clients["auditor"].get(f"/api/bags/{new_bag}").get_json()["destination_fps"]
    assert old_view["at_registration"]["name"] == original and old_view["identity_changed_since"] is True
    assert new_view["at_registration"]["name"] == "FPS renamed after registration"
    assert new_view["identity_changed_since"] is False
    assert old_view["name"] == new_view["name"] == "FPS renamed after registration"    # current identity
    page = clients["admin"].get(f"/b/{bag}").get_data(as_text=True)
    assert f"At registration: {original}" in page

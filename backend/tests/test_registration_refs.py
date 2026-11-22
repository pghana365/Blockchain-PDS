"""G2 / G3 / G6 and safeguards C and D: consignment references, collision retries, consignment
selection, recovery consistency, and what reaches the ledger."""
import re

import pytest

from app.extensions import db
from app.models import Bag, BlockchainTransaction, Consignment, Correction, FpsLocation, MlsLocation
from app.services import workflow
from app.utils import shop_codes

from conftest import dispatch, receive

REF_RE = re.compile(r"^CNS-[A-HJ-NP-Z2-9]{12}$")
LEDGER_RE = re.compile(r"^CONS-[A-HJ-NP-Z2-9]{12}$")


def _ids():
    return (MlsLocation.query.filter_by(code="MLS-01").one().id, FpsLocation.query.filter_by(code="FPS-01").one().id,
            MlsLocation.query.filter_by(code="MLS-02").one().id, FpsLocation.query.filter_by(code="FPS-02").one().id)


def _register(clients, **body):
    m1, f1, _, _ = _ids()
    return clients["admin"].post("/api/admin/bags", json={"mls_id": m1, "fps_id": f1} | body)


def test_references_are_12_random_chars_from_the_csprng(monkeypatch):
    refs = {shop_codes.new_consignment_reference() for _ in range(3000)}
    assert len(refs) == 3000 and all(REF_RE.match(r) for r in refs)
    assert all(LEDGER_RE.match(shop_codes.new_ledger_consignment_id()) for _ in range(100))
    calls = []
    monkeypatch.setattr(shop_codes.secrets, "choice", lambda seq: calls.append(1) or seq[0])
    assert shop_codes.new_consignment_reference() == "CNS-" + "A" * 12 and len(calls) == 12


def test_new_consignment_has_independent_reference_and_ledger_id(app, clients, ledger):
    r = _register(clients)
    assert r.status_code == 201
    cons = Consignment.query.filter_by(reference=r.get_json()["consignment_ref"]).one()
    assert REF_RE.match(cons.reference) and LEDGER_RE.match(cons.code)
    assert cons.reference[4:] != cons.code[5:]                         # drawn independently
    args = ledger.calls[-1]["args"]
    assert args[4] == cons.code and cons.reference not in args


def test_collisions_are_retried_and_never_duplicate(app, clients, monkeypatch):
    r = _register(clients)
    taken = Consignment.query.filter_by(reference=r.get_json()["consignment_ref"]).one()
    seq = iter([taken.reference, taken.reference, "CNS-" + "Q" * 12])
    monkeypatch.setattr(workflow, "new_consignment_reference", lambda: next(seq))
    r2 = _register(clients)
    assert r2.status_code == 201 and r2.get_json()["consignment_ref"] == "CNS-" + "Q" * 12
    lseq = iter([taken.code, "CONS-" + "W" * 12])
    monkeypatch.setattr(workflow, "new_consignment_reference", shop_codes.new_consignment_reference)
    monkeypatch.setattr(workflow, "new_ledger_consignment_id", lambda: next(lseq))
    assert _register(clients).status_code == 201                       # ledger-id collision retried too
    refs = [c.reference for c in Consignment.query.all()]
    codes = [c.code for c in Consignment.query.all()]
    assert len(refs) == len(set(refs)) and len(codes) == len(set(codes)) and len(refs) == 3


def test_persistent_collisions_fail_cleanly_without_partial_records(app, clients, monkeypatch):
    r = _register(clients)
    taken = r.get_json()["consignment_ref"]
    before = (Bag.query.count(), Consignment.query.count(), BlockchainTransaction.query.count())
    monkeypatch.setattr(workflow, "new_consignment_reference", lambda: taken)
    r = _register(clients, count=3)
    assert r.status_code == 503 and r.get_json()["code"] == "RETRY"
    assert (Bag.query.count(), Consignment.query.count(), BlockchainTransaction.query.count()) == before


def test_bulk_bags_share_one_new_consignment_and_can_be_extended(app, clients, ledger):
    r = _register(clients, count=4)
    assert r.status_code == 201
    ref = r.get_json()["consignment_ref"]
    bags = [c["bag_id"] for c in r.get_json()["created"]]
    assert len(bags) == 4 and {Bag.query.filter_by(bag_id=b).one().consignment.reference for b in bags} == {ref}
    r = _register(clients, consignment_ref=ref.lower(), count=2)        # add to the existing consignment
    assert r.status_code == 201 and r.get_json()["consignment_ref"] == ref
    assert Bag.query.join(Consignment, Consignment.id == Bag.consignment_id).filter(
        Consignment.reference == ref).count() == 6
    assert len({c["args"][4] for c in ledger.calls if c["function"] == "RegisterBag" and c["args"][0] in bags}) == 1


def test_existing_consignment_route_is_enforced(app, clients):
    ref = _register(clients).get_json()["consignment_ref"]
    _, _, m2, f2 = _ids()
    m1, f1, _, _ = _ids()
    for body in ({"mls_id": m2, "fps_id": f1}, {"mls_id": m1, "fps_id": f2}):
        r = clients["admin"].post("/api/admin/bags", json=body | {"consignment_ref": ref})
        assert r.status_code == 409 and r.get_json()["code"] == "CONSIGNMENT_MISMATCH"
    r = _register(clients, consignment_ref="CNS-NOSUCHREF00")
    assert r.status_code == 404 and r.get_json()["code"] == "CONSIGNMENT_NOT_FOUND"


@pytest.mark.parametrize("body", [{"consignment_id": "CONS-NALGONDA-MIRYALAGUDA"},
                                  {"consignment_id": "MY-OWN-ID"},
                                  {"commodity": "Nalgonda rice"}, {"commodity": "FPS-01 rice"}])
def test_free_text_that_would_reach_the_ledger_is_rejected(app, clients, ledger, body):
    before = (Bag.query.count(), Consignment.query.count(), len(ledger.calls))
    assert _register(clients, **body).status_code == 400
    assert (Bag.query.count(), Consignment.query.count(), len(ledger.calls)) == before


def test_failed_registration_leaves_no_orphans(app, clients, ledger):
    before = (Bag.query.count(), Consignment.query.count())
    ledger.mode["RegisterBag"] = "invalid"            # submit outcome unknown; the ledger then reports INVALID
    r = _register(clients)
    assert r.status_code == 409                       # confirmed rejected while polling: definite failure
    ledger.mode.clear()
    assert (Bag.query.count(), Consignment.query.count()) == before   # bag AND its new consignment undone
    op = BlockchainTransaction.query.filter_by(function="RegisterBag", status="FAILED").one()
    assert op.bag_pk is None                                          # attempt still visible in history


def test_failed_bag_in_existing_consignment_keeps_the_consignment(app, clients, ledger):
    ref = _register(clients).get_json()["consignment_ref"]
    ledger.fail_functions.add("RegisterBag")
    assert _register(clients, consignment_ref=ref).status_code >= 400
    ledger.fail_functions.clear()
    assert Consignment.query.filter_by(reference=ref).one()


def test_unknown_outcome_registration_recovers_to_one_consistent_record(app, clients, ledger):
    ledger.mode["RegisterBag"] = "unknown_pending"
    r = _register(clients, count=3)
    assert r.status_code == 202
    bags = [c["bag_id"] for c in r.get_json()["created"]]
    ledger.commit_pending()
    ledger.mode.clear()
    for b in bags:
        clients["admin"].post(f"/api/admin/bags/{b}/resolve-pending", json={})
    refs = {Bag.query.filter_by(bag_id=b).one().consignment.reference for b in bags}
    assert len(refs) == 1 and all(Bag.query.filter_by(bag_id=b).one().register_tx_id for b in bags)
    assert all(b in ledger.chain for b in bags)
    assert BlockchainTransaction.query.filter_by(status="PENDING").count() == 0


def test_ledger_arguments_and_corrections_carry_no_location_information(app, clients, ledger, bag):
    dispatch(clients, bag, "50.0")
    receive(clients, bag, "46.0")
    note = "Scale at Fair Price Shop 0123 - Avanthipuram, Miryalaguda was recalibrated"
    r = clients["admin"].post(f"/api/admin/bags/{bag}/corrections", json={"note": note})
    assert r.status_code == 201
    corr = Correction.query.one()
    assert corr.note == note and corr.note_salt
    on_chain = next(c["args"] for c in ledger.calls if c["function"] == "RecordCorrection")
    assert on_chain[1] == workflow.correction_commitment(bag, note, corr.note_salt)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", on_chain[1])
    forbidden = set()
    for loc in MlsLocation.query.all() + FpsLocation.query.all():
        forbidden |= {loc.name, loc.mandal, loc.district, loc.shop_code} | ({loc.village} if getattr(loc, "village", None) else set())
    forbidden |= {c.reference for c in Consignment.query.all()} | {"Avanthipuram", "Miryalaguda", "Nalgonda", "recalibrated"}
    for call in ledger.calls:
        for arg in call["args"]:
            for s in forbidden:
                assert s not in str(arg), (call["function"], s)

"""G3 / safeguard C under real concurrency: simultaneous registrations never share or duplicate a
consignment reference, collisions are retried, and bulk registration stays intact.

Runs against the project's MySQL server (database `pds_test`) because row locks and UNIQUE-index
races cannot be reproduced in SQLite. Skipped when MySQL is not reachable.
"""
import threading

import pytest

from app import create_app
from app.extensions import db
from app.models import Bag, BlockchainTransaction, Consignment, FpsLocation, MlsLocation, User
from app.services import workflow
from app.utils import shop_codes
from config import TestConfig

from conftest import FakeLedger
from test_audit_concurrency import MYSQL_URL

pytestmark = pytest.mark.skipif(MYSQL_URL is None, reason="MySQL test database not reachable")
COLLIDING = "CNS-" + "C" * 12


@pytest.fixture()
def mysql_app():
    class MySqlConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = MYSQL_URL
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": 20, "pool_pre_ping": True}

    app = create_app(MySqlConfig)
    with app.app_context():
        db.drop_all()
        db.create_all()
        app.test_cli_runner().invoke(args=["seed"])
    app.extensions["ledger"] = FakeLedger()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_concurrent_registrations_get_unique_references_despite_forced_collisions(mysql_app, monkeypatch):
    # every thread's FIRST draw is the same reference: all but one must collide and retry
    local = threading.local()

    def colliding_first():
        if not getattr(local, "drawn", False):
            local.drawn = True
            return COLLIDING
        return shop_codes.new_consignment_reference()

    monkeypatch.setattr(workflow, "new_consignment_reference", colliding_first)
    threads_n, results, errors = 10, [], []
    barrier = threading.Barrier(threads_n)

    def worker(i):
        try:
            with mysql_app.app_context():
                admin = User.query.filter_by(username="admin").one()
                m = MlsLocation.query.filter_by(code="MLS-01" if i % 2 else "MLS-02").one()
                f = FpsLocation.query.filter_by(code="FPS-01" if i % 2 else "FPS-02").one()
                barrier.wait()
                if i % 3 == 0:
                    bags, outcome = workflow.register_bags(admin, m.id, f.id, 3)
                else:
                    bags, outcome = [workflow.register_bag(admin, m.id, f.id)], workflow.OUTCOME_CONFIRMED
                results.append((i, [b.bag_id for b in bags], {b.consignment.reference for b in bags}, outcome))
                db.session.remove()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(f"{i}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len(results) == threads_n

    with mysql_app.app_context():
        refs = [c.reference for c in Consignment.query.all()]
        codes = [c.code for c in Consignment.query.all()]
        assert len(refs) == threads_n and len(set(refs)) == threads_n and len(set(codes)) == threads_n
        assert refs.count(COLLIDING) == 1                       # exactly one thread won the colliding draw
        for i, bag_ids, bag_refs, outcome in results:
            assert len(bag_refs) == 1 and outcome == workflow.OUTCOME_CONFIRMED   # one consignment per call
            assert len(bag_ids) == (3 if i % 3 == 0 else 1)
        all_bags = [b for _, ids, _, _ in results for b in ids]
        assert len(all_bags) == len(set(all_bags)) == Bag.query.count()
        for b in Bag.query.all():                               # route of every bag == route of its consignment
            assert (b.mls_id, b.fps_id) == (b.consignment.mls_id, b.consignment.fps_id) and b.register_tx_id
        assert BlockchainTransaction.query.filter_by(status="PENDING").count() == 0
        assert BlockchainTransaction.query.filter_by(function="RegisterBag", status="COMMITTED").count() == len(all_bags)
    assert len([c for c in mysql_app.extensions["ledger"].calls if c["function"] == "RegisterBag"]) == len(all_bags)


def test_concurrent_adds_to_one_existing_consignment(mysql_app):
    with mysql_app.app_context():
        admin = User.query.filter_by(username="admin").one()
        m = MlsLocation.query.filter_by(code="MLS-01").one()
        f = FpsLocation.query.filter_by(code="FPS-01").one()
        ref = workflow.register_bag(admin, m.id, f.id).consignment.reference
        m_id, f_id = m.id, f.id
        db.session.remove()
    errors, barrier = [], threading.Barrier(6)

    def worker():
        try:
            with mysql_app.app_context():
                admin = User.query.filter_by(username="admin").one()
                barrier.wait()
                workflow.register_bags(admin, m_id, f_id, 2, "Rice", ref)
                db.session.remove()
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    with mysql_app.app_context():
        assert Consignment.query.count() == 1
        assert Bag.query.count() == 13 and {b.consignment.reference for b in Bag.query.all()} == {ref}
        ids = [b.bag_id for b in Bag.query.all()]
        assert len(ids) == len(set(ids))

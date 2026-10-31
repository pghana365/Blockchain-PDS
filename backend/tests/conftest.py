import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import FpsLocation, MlsLocation  # noqa: E402
from app.services.ledger import LedgerError  # noqa: E402
from config import TestConfig  # noqa: E402

IDENTITIES = {
    "pdsadmin": ("admin", ""), "mls01op": ("mls", "MLS-01"), "mls02op": ("mls", "MLS-02"),
    "fps01op": ("fps", "FPS-01"), "fps02op": ("fps", "FPS-02"), "auditor1": ("auditor", ""),
}
PASSWORD = TestConfig.SEED_PASSWORD


class FakeLedger:
    """Stands in for the Node gateway. (Chaincode rules are tested in fabric/chaincode/pds-contract/test.)"""

    def __init__(self):
        self.calls = []
        self.fail_functions = set()
        self._n = itertools.count(1)

    def submit(self, identity, function, args):
        if function in self.fail_functions:
            raise LedgerError("simulated ledger outage", 503)
        n = next(self._n)
        self.calls.append({"identity": identity, "function": function, "args": list(args)})
        return {"txId": f"{n:064x}", "blockNumber": str(n), "result": {}}

    def evaluate(self, identity, function, args):
        raise LedgerError("not available in unit tests", 503)

    def identity(self, name):
        if name in IDENTITIES:
            role, loc = IDENTITIES[name]
            return {"exists": True, "role": role, "location": loc}
        return {"exists": False}


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        app.test_cli_runner().invoke(args=["seed"])
        app.extensions["ledger"] = FakeLedger()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def ledger(app):
    return app.extensions["ledger"]


def make_client(app, username, password=PASSWORD):
    c = app.test_client()
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 302, r.data
    return c


@pytest.fixture()
def clients(app):
    return {
        "admin": make_client(app, "admin"),
        "mls1": make_client(app, "mls01.operator"),
        "mls2": make_client(app, "mls02.operator"),
        "fps1": make_client(app, "fps01.operator"),
        "fps2": make_client(app, "fps02.operator"),
        "auditor": make_client(app, "auditor1"),
    }


@pytest.fixture()
def bag(app, clients):
    """BAG-0001: registered by admin, MLS-01 -> FPS-01."""
    mls = MlsLocation.query.filter_by(code="MLS-01").one()
    fps = FpsLocation.query.filter_by(code="FPS-01").one()
    r = clients["admin"].post("/api/admin/bags", json={"mls_id": mls.id, "fps_id": fps.id})
    assert r.status_code == 201, r.data
    return r.get_json()["created"][0]["bag_id"]


def dispatch(clients, bag_id, weight, who="mls1"):
    return clients[who].post(f"/api/bags/{bag_id}/dispatch", json={"weight_kg": weight})


def receive(clients, bag_id, weight, who="fps1"):
    return clients[who].post(f"/api/bags/{bag_id}/receive", json={"weight_kg": weight})

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
    """Stands in for the Node gateway / Fabric in UNIT tests. It is a fake, not Fabric:
    it keeps a tiny on-chain state per bag and can simulate the gateway's outcome-unknown cases.
    (Chaincode rules are tested in fabric/chaincode/pds-contract/test; real-network behaviour is
    exercised separately against the running Fabric network.)

    mode[function] values:
      'unknown_committed'  tx commits, but the caller gets "commit status unknown" (with the tx id)
      'unknown_pending'    tx is in flight: unknown now, commits later via commit_pending()
      'unknown_lost'       caller gets "unknown" with NO tx id and nothing commits (e.g. HTTP timeout, never arrived)
      'unknown_lost_committed'  caller gets "unknown" with NO tx id although the tx did commit
      'invalid'            caller gets "unknown"; the ledger later reports the tx INVALID
    """

    def __init__(self):
        self.calls = []
        self.fail_functions = set()
        self.mode = {}
        self.chain = {}            # bag_id -> on-chain bag record (same keys as the chaincode)
        self.committed = {}        # tx_id -> block number
        self.invalid = set()
        self.in_flight = []        # (function, args, tx_id, block) waiting to commit
        self.unreachable = False   # evaluate / tx_status unavailable
        self.tx_status_calls = 0
        self.commit_after_polls = 0   # >0: in-flight transactions commit once tx_status was polled this many times
        self._n = itertools.count(1)

    # ---- chain state ----------------------------------------------------------------------
    def _apply(self, function, args, tx):
        if function == "RegisterBag":
            self.chain[args[0]] = {"bagId": args[0], "status": "REGISTERED", "registerTxId": tx}
        elif function == "DispatchBag":
            self.chain[args[0]].update(status="IN_TRANSIT", dispatchCommitment=args[1], dispatchTxId=tx)
        elif function == "ReceiveBag":
            self.chain[args[0]].update(status="RECEIVED", receiptCommitment=args[1], receiptTxId=tx)
        elif function == "RecordReconciliation":
            self.chain[args[0]]["status"] = args[1]
            self.chain[args[0]]["reconciliation"] = {"result": args[1], "differenceKg": args[2],
                                                      "toleranceKg": args[3], "txId": tx}

    def _commit(self, function, args, tx, block):
        self._apply(function, args, tx)
        self.committed[tx] = block
        self.calls.append({"identity": None, "function": function, "args": list(args), "tx": tx})

    def commit_pending(self):
        """The delayed block arrives: everything in flight is now committed."""
        for function, args, tx, block in self.in_flight:
            self._commit(function, args, tx, block)
        self.in_flight.clear()

    # ---- gateway API ----------------------------------------------------------------------
    def submit(self, identity, function, args):
        if function in self.fail_functions:
            raise LedgerError("simulated ledger outage", 503)
        n = next(self._n)
        tx = f"{n:064x}"
        mode = self.mode.get(function)
        if mode == "unknown_committed":
            self._commit(function, args, tx, n)
            raise LedgerError("Commit status could not be confirmed", 502, "UNKNOWN", tx)
        if mode == "unknown_pending":
            self.in_flight.append((function, list(args), tx, n))
            raise LedgerError("Commit status could not be confirmed", 502, "UNKNOWN", tx)
        if mode == "unknown_lost":
            raise LedgerError("The gateway did not answer in time; the outcome is unknown.", 503, "UNKNOWN", None)
        if mode == "unknown_lost_committed":
            self._commit(function, args, tx, n)
            raise LedgerError("The gateway did not answer in time; the outcome is unknown.", 503, "UNKNOWN", None)
        if mode == "invalid":
            self.invalid.add(tx)
            raise LedgerError("Commit status could not be confirmed", 502, "UNKNOWN", tx)
        self._commit(function, args, tx, n)
        self.calls[-1]["identity"] = identity
        return {"txId": tx, "blockNumber": str(n), "result": {}}

    def tx_status(self, identity, tx_id):
        self.tx_status_calls += 1
        if self.unreachable:
            raise LedgerError("gateway unavailable", 503)
        if self.commit_after_polls and self.in_flight and self.tx_status_calls >= self.commit_after_polls:
            self.commit_pending()                       # the delayed block arrives while we are polling
        if tx_id in self.invalid:
            return {"state": "INVALID", "code": "MVCC_READ_CONFLICT"}
        if tx_id in self.committed:
            return {"state": "VALID", "blockNumber": str(self.committed[tx_id])}
        return {"state": "NOT_FOUND"}

    def evaluate(self, identity, function, args):
        if self.unreachable:
            raise LedgerError("gateway unavailable", 503)
        if function == "GetBagStatus":
            if args[0] not in self.chain:
                raise LedgerError(f"chaincode response 500, BAG_NOT_FOUND: {args[0]}", 502)
            return {"result": dict(self.chain[args[0]])}
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

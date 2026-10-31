from flask import Blueprint, jsonify
from flask_login import current_user

from ..middleware.auth import roles_required
from ..models import Bag, DispatchRecord, ReceiptRecord, ReconciliationRecord
from ..models.constants import ROLE_ADMIN, ROLE_AUDITOR
from ..services import audit, workflow
from ..services.ledger import chain_evaluate, commitment, fmt_weight
from .common import api_guard

bp = Blueprint("api_audit", __name__, url_prefix="/api/audit")


@bp.get("/bags/<bag_id>/verify")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
@api_guard
def verify_bag(bag_id):
    """Cross-check MySQL against the Fabric ledger.

    Proves: the weights stored in MySQL still match the commitments that were
    written to the immutable ledger at the time, and the on-chain state agrees
    with the database. It does NOT prove the physical weight was measured right.
    """
    bag_id = workflow.normalize_bag_id(bag_id)
    bag = Bag.query.filter_by(bag_id=bag_id).one_or_none()
    if not bag:
        raise workflow.WorkflowError("Bag not found", 404, "BAG_NOT_FOUND")
    chain = chain_evaluate(current_user, "GetBagStatus", [bag_id])["result"]
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("Bag exists on ledger", True, f"on-chain status {chain['status']}")
    add("Status matches database", chain["status"] == bag.status, f"db={bag.status} chain={chain['status']}")

    d = DispatchRecord.query.filter_by(bag_pk=bag.id).first()
    if d:
        local = commitment(bag_id, d.weight_kg, d.salt)
        add("Dispatch weight in MySQL matches stored commitment", local == d.commitment)
        add("Dispatch commitment matches ledger", local == chain.get("dispatchCommitment"),
            "weight unchanged since dispatch" if local == chain.get("dispatchCommitment") else "MISMATCH - record altered?")
        add("Dispatch tx id matches ledger", d.tx_id == chain.get("dispatchTxId"))
    elif chain.get("dispatchCommitment"):
        add("Dispatch record present in MySQL", False, "ledger has a dispatch but the database does not")

    r = ReceiptRecord.query.filter_by(bag_pk=bag.id).first()
    if r:
        local = commitment(bag_id, r.weight_kg, r.salt)
        add("Receipt weight in MySQL matches stored commitment", local == r.commitment)
        add("Receipt commitment matches ledger", local == chain.get("receiptCommitment"),
            "weight unchanged since receipt" if local == chain.get("receiptCommitment") else "MISMATCH - record altered?")
        add("Receipt tx id matches ledger", r.tx_id == chain.get("receiptTxId"))
    elif chain.get("receiptCommitment"):
        add("Receipt record present in MySQL", False, "ledger has a receipt but the database does not")

    c = ReconciliationRecord.query.filter_by(bag_pk=bag.id).first()
    onchain = chain.get("reconciliation")
    if c and d and r:
        diff = abs(d.weight_kg - r.weight_kg)
        add("Recomputed difference matches stored difference", fmt_weight(diff) == fmt_weight(c.difference_kg))
        if onchain:
            add("Result matches ledger", onchain["result"] == c.status and onchain["differenceKg"] == fmt_weight(c.difference_kg),
                f"db={c.status}/{fmt_weight(c.difference_kg)} chain={onchain['result']}/{onchain['differenceKg']}")
        else:
            add("Reconciliation recorded on ledger", False, "pending - use 'retry chain sync' (admin)")
    ok = all(x["ok"] for x in checks)
    audit.log("INTEGRITY_VERIFIED", current_user, "bag", bag_id, {"ok": ok}, commit=True)
    return jsonify(bag_id=bag_id, ok=ok, checks=checks)


@bp.get("/bags/<bag_id>/chain-history")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
@api_guard
def chain_history(bag_id):
    bag_id = workflow.normalize_bag_id(bag_id)
    if not Bag.query.filter_by(bag_id=bag_id).first():
        raise workflow.WorkflowError("Bag not found", 404, "BAG_NOT_FOUND")
    hist = chain_evaluate(current_user, "GetTransactionHistory", [bag_id])["result"]
    # Fabric's smoke test showed the ledger returns newest-first; present oldest-first
    # (stable: timestamps have 1-second resolution so sorting by time could reorder ties)
    hist = list(reversed(hist))
    return jsonify([{"tx_id": h["txId"], "timestamp": h["timestamp"],
                     "status": (h.get("value") or {}).get("status")} for h in hist])


@bp.get("/log-integrity")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
@api_guard
def log_integrity():
    ok, count, bad = audit.verify_chain()
    return jsonify(ok=ok, entries_checked=count, first_bad_entry=bad)

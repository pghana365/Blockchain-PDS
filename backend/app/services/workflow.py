"""Business workflow. Every rule is enforced here on the server."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (Bag, Consignment, Correction, DispatchRecord, FpsLocation, MlsLocation,
                      ReceiptRecord, ReconciliationRecord)
from ..models.constants import (MAX_BAG_WEIGHT_KG, STATUS_FLAGGED, STATUS_IN_TRANSIT, STATUS_MATCHED,
                                STATUS_REGISTERED, utcnow)
from . import audit
from .ledger import (LedgerError, chain_submit, commitment, fmt_weight, new_request_id, new_salt)
from .settings import get_tolerance

BAG_ID_RE = re.compile(r"^BAG-\d{4,}$")
FLAG_MESSAGE = ("Discrepancy flagged: this requires further verification. "
                "A weight difference does not by itself indicate theft or fraud.")
MATCH_MESSAGE = "Weights are within the allowed tolerance."


class WorkflowError(Exception):
    def __init__(self, message, status=400, code="ERROR"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


# ----------------------------------------------------------------- validation
def normalize_bag_id(raw) -> str:
    bag_id = str(raw or "").strip().upper()
    if not BAG_ID_RE.match(bag_id):
        raise WorkflowError("Bag ID must look like BAG-0001", 400, "INVALID_BAG_ID")
    return bag_id


def parse_weight(raw) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise WorkflowError("Weight is required", 400, "INVALID_WEIGHT")
    try:
        w = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise WorkflowError("Weight must be a number in kilograms", 400, "INVALID_WEIGHT")
    if not w.is_finite() or w <= 0:
        raise WorkflowError("Weight must be greater than 0 kg", 400, "INVALID_WEIGHT")
    if w > MAX_BAG_WEIGHT_KG:
        raise WorkflowError(f"Weight must not exceed {MAX_BAG_WEIGHT_KG} kg", 400, "INVALID_WEIGHT")
    if w != w.quantize(Decimal("0.001")):
        raise WorkflowError("Weight may have at most 3 decimal places", 400, "INVALID_WEIGHT")
    return w.quantize(Decimal("0.001"))


def reconcile_weights(dispatch_w: Decimal, receipt_w: Decimal, tolerance: Decimal):
    """difference = |dispatch - receipt|; MATCHED if <= tolerance else FLAGGED."""
    diff = abs(Decimal(dispatch_w) - Decimal(receipt_w)).quantize(Decimal("0.001"))
    return diff, (STATUS_MATCHED if diff <= Decimal(tolerance) else STATUS_FLAGGED)


def _locked_bag(bag_id: str) -> Bag:
    bag = Bag.query.filter_by(bag_id=bag_id).with_for_update().one_or_none()
    if bag is None:
        raise WorkflowError(f"Bag {bag_id} does not exist", 404, "BAG_NOT_FOUND")
    return bag


def _require_operator(user, kind):
    if not user.is_enabled:
        raise WorkflowError("Account disabled", 403, "FORBIDDEN")
    if kind == "mls" and not (user.is_mls and user.mls_location_id):
        raise WorkflowError("Only an MLS operator with an assigned MLS can do this", 403, "FORBIDDEN")
    if kind == "fps" and not (user.is_fps and user.fps_location_id):
        raise WorkflowError("Only an FPS operator with an assigned FPS can do this", 403, "FORBIDDEN")


# ------------------------------------------------------------------- register
def next_bag_id() -> str:
    n = 0
    for (bid,) in db.session.query(Bag.bag_id).all():
        m = re.match(r"^BAG-(\d+)$", bid)
        if m:
            n = max(n, int(m.group(1)))
    return f"BAG-{n + 1:04d}"


def register_bag(admin, mls_id, fps_id, bag_id=None, commodity="Rice", consignment_code=None):
    if not admin.is_admin:
        raise WorkflowError("Only an administrator can register bags", 403, "FORBIDDEN")
    mls, fps = db.session.get(MlsLocation, mls_id), db.session.get(FpsLocation, fps_id)
    if not mls or not mls.is_active or not fps or not fps.is_active:
        raise WorkflowError("Choose an active MLS and an active destination FPS", 400, "INVALID_LOCATION")
    commodity = (commodity or "Rice").strip()
    if not commodity or len(commodity) > 50:
        raise WorkflowError("Commodity must be 1-50 characters", 400, "INVALID_INPUT")
    bag_id = normalize_bag_id(bag_id) if bag_id else next_bag_id()
    if Bag.query.filter_by(bag_id=bag_id).first():
        raise WorkflowError(f"{bag_id} is already registered", 409, "DUPLICATE_BAG")

    code = (consignment_code or f"CONS-{utcnow():%Y%m%d}-{mls.code}-{fps.code}").strip().upper()
    if not re.match(r"^[A-Z0-9-]{3,40}$", code):
        raise WorkflowError("Consignment ID: 3-40 characters A-Z, 0-9, '-'", 400, "INVALID_INPUT")
    cons = Consignment.query.filter_by(code=code).first()
    if cons and (cons.mls_id != mls.id or cons.fps_id != fps.id):
        raise WorkflowError("That consignment already exists for a different MLS/FPS", 409, "CONSIGNMENT_MISMATCH")
    if not cons:
        cons = Consignment(code=code, mls_id=mls.id, fps_id=fps.id, created_by=admin.id)
        db.session.add(cons)
        db.session.flush()

    bag = Bag(bag_id=bag_id, commodity=commodity, consignment_id=cons.id, mls_id=mls.id, fps_id=fps.id,
              status=STATUS_REGISTERED, created_by=admin.id)
    db.session.add(bag)
    db.session.flush()
    rid = new_request_id()
    out = chain_submit(admin, bag, "RegisterBag", [bag_id, commodity, mls.code, fps.code, cons.code, rid], rid)
    bag.register_tx_id = out["txId"]
    audit.log("BAG_REGISTERED", admin, "bag", bag_id, {"mls": mls.code, "fps": fps.code, "tx": out["txId"]})
    db.session.commit()
    return bag


# ------------------------------------------------------------------- dispatch
def dispatch_bag(user, bag_id, weight_raw):
    _require_operator(user, "mls")
    bag_id = normalize_bag_id(bag_id)
    weight = parse_weight(weight_raw)
    bag = _locked_bag(bag_id)
    if bag.mls_id != user.mls_location_id:
        raise WorkflowError("This bag does not belong to your MLS location", 403, "WRONG_LOCATION")
    already = db.session.query(DispatchRecord.id).filter_by(bag_pk=bag.id).first()
    if already or bag.status != STATUS_REGISTERED:
        raise WorkflowError(f"{bag_id} has already been dispatched (status {bag.status})", 409, "ALREADY_DISPATCHED")

    salt, rid = new_salt(), new_request_id()
    commit_hash = commitment(bag_id, weight, salt)
    rec = DispatchRecord(bag_pk=bag.id, weight_kg=weight, salt=salt, commitment=commit_hash,
                         operator_id=user.id, mls_id=bag.mls_id, request_id=rid)
    db.session.add(rec)
    try:
        db.session.flush()  # UNIQUE(bag_pk) guards against concurrent duplicate dispatch
    except IntegrityError:
        db.session.rollback()
        raise WorkflowError(f"{bag_id} has already been dispatched", 409, "ALREADY_DISPATCHED")

    out = chain_submit(user, bag, "DispatchBag", [bag_id, commit_hash, rid], rid)
    rec.tx_id = out["txId"]
    bag.status = STATUS_IN_TRANSIT
    audit.log("BAG_DISPATCHED", user, "bag", bag_id, {"tx": out["txId"], "mls": user.mls_location.code})
    db.session.commit()
    return bag, rec


# -------------------------------------------------------------------- receive
def receive_bag(user, bag_id, weight_raw):
    _require_operator(user, "fps")
    bag_id = normalize_bag_id(bag_id)
    weight = parse_weight(weight_raw)
    bag = _locked_bag(bag_id)
    if bag.fps_id != user.fps_location_id:
        raise WorkflowError("This bag is not assigned to your FPS", 403, "WRONG_LOCATION")
    already = db.session.query(ReceiptRecord.id).filter_by(bag_pk=bag.id).first()
    if already:
        raise WorkflowError(f"A receipt for {bag_id} has already been submitted", 409, "ALREADY_RECEIVED")
    if bag.status != STATUS_IN_TRANSIT:
        raise WorkflowError(f"{bag_id} is {bag.status}; only IN_TRANSIT bags can be received", 409, "INVALID_STATE")

    salt, rid = new_salt(), new_request_id()
    commit_hash = commitment(bag_id, weight, salt)
    rec = ReceiptRecord(bag_pk=bag.id, weight_kg=weight, salt=salt, commitment=commit_hash,
                        operator_id=user.id, fps_id=bag.fps_id, request_id=rid)
    db.session.add(rec)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise WorkflowError(f"A receipt for {bag_id} has already been submitted", 409, "ALREADY_RECEIVED")

    out = chain_submit(user, bag, "ReceiveBag", [bag_id, commit_hash, rid], rid)
    rec.tx_id = out["txId"]
    bag.status = "RECEIVED"
    audit.log("BAG_RECEIVED", user, "bag", bag_id, {"tx": out["txId"], "fps": user.fps_location.code})
    db.session.commit()  # receipt is now final in both DB and ledger

    # ---- reconciliation (server-side only; the FPS operator never sees the inputs beforehand)
    bag_pk, user_id = bag.id, user.id
    dispatch = DispatchRecord.query.filter_by(bag_pk=bag_pk).one()   # internal access, not user-facing
    tolerance = get_tolerance()
    diff, result = reconcile_weights(dispatch.weight_kg, weight, tolerance)
    recon = ReconciliationRecord(bag_pk=bag_pk, dispatch_weight_kg=dispatch.weight_kg, receipt_weight_kg=weight,
                                 difference_kg=diff, tolerance_kg=tolerance, status=result, chain_status="PENDING")
    db.session.add(recon)
    bag = db.session.get(Bag, bag_pk)
    bag.status = result
    audit.log("BAG_RECONCILED", user, "bag", bag_id, {"result": result, "difference_kg": str(diff), "tolerance_kg": str(tolerance)})
    db.session.commit()

    sync_reconciliation_to_chain(user, bag_id)
    return db.session.get(Bag, bag_pk)


def sync_reconciliation_to_chain(actor, bag_id) -> bool:
    """Write the reconciliation result to Fabric. Failure leaves chain_status=PENDING (admin can retry)."""
    bag = Bag.query.filter_by(bag_id=bag_id).one()
    recon = ReconciliationRecord.query.filter_by(bag_pk=bag.id).one()
    if recon.chain_status == "CONFIRMED":
        return True
    rid = new_request_id()
    try:
        out = chain_submit(actor, bag, "RecordReconciliation",
                           [bag_id, recon.status, fmt_weight(recon.difference_kg), fmt_weight(recon.tolerance_kg), rid], rid)
    except LedgerError as e:
        audit.log("CHAIN_SYNC_FAILED", actor, "bag", bag_id, {"error": e.message[:300]}, commit=True)
        return False
    recon = ReconciliationRecord.query.filter_by(bag_pk=bag.id).one()
    recon.chain_status, recon.chain_tx_id = "CONFIRMED", out["txId"]
    db.session.commit()
    return True


# ---------------------------------------------------------------- corrections
def add_correction(admin, bag_id, note):
    if not admin.is_admin:
        raise WorkflowError("Only an administrator can record corrections", 403, "FORBIDDEN")
    note = (note or "").strip()
    if not 5 <= len(note) <= 500:
        raise WorkflowError("Correction note must be 5-500 characters", 400, "INVALID_INPUT")
    bag = _locked_bag(normalize_bag_id(bag_id))
    rid = new_request_id()
    out = chain_submit(admin, bag, "RecordCorrection", [bag.bag_id, note, rid], rid)
    c = Correction(bag_pk=bag.id, note=note, admin_id=admin.id, tx_id=out["txId"])
    db.session.add(c)
    audit.log("CORRECTION_RECORDED", admin, "bag", bag.bag_id, {"note": note, "tx": out["txId"]})
    db.session.commit()
    return c

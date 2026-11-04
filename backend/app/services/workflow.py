"""Business workflow. Every rule is enforced here on the server.

MySQL and Fabric cannot share one atomic transaction, so every bag write is a small saga
(a "write-ahead" pattern):

  1. In ONE MySQL commit: validate, save the intended record (tx_id still empty) and a
     blockchain_transactions row with status PENDING.     <- survives a crash from here on
  2. Send the transaction to Fabric and obtain its VERIFIED outcome (ledger.submit_tx).
  3. "Settle" the PENDING row with that outcome, in one MySQL commit:
       COMMITTED -> fill in tx_id, advance the bag state, create the reconciliation (receipts)
       FAILED    -> certainly not on the ledger: remove the intended record again
       UNKNOWN   -> leave everything PENDING. Nothing is rolled back or resent; the ledger is
                    asked later (resolve_pending) using the tx id and the on-chain bag state.

A bag only changes state, and a transaction is only reported as successful, after the ledger
has confirmed it. Settling and resolving are idempotent, so retries cannot create duplicate
receipts, reconciliations or Fabric transactions.
"""
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (Bag, BlockchainTransaction, Consignment, Correction, DispatchRecord, FpsLocation,
                      MlsLocation, ReceiptRecord, ReconciliationRecord, User)
from ..models.constants import (MAX_BAG_WEIGHT_KG, STATUS_FLAGGED, STATUS_IN_TRANSIT, STATUS_MATCHED,
                                STATUS_RECEIVED, STATUS_REGISTERED, utcnow)
from . import audit
from .ledger import (COMMITTED, FAILED, UNKNOWN, LedgerError, TxResult, chain_submit, commitment, fmt_weight,
                     get_ledger, new_request_id, new_salt, submit_tx, tx_status)
from .settings import get_tolerance

BAG_ID_RE = re.compile(r"^BAG-\d{4,}$")
FLAG_MESSAGE = ("Discrepancy flagged: this requires further verification. "
                "A weight difference does not by itself indicate theft or fraud.")
MATCH_MESSAGE = "Weights are within the allowed tolerance."

PENDING = "PENDING"                       # blockchain_transactions.status of an unsettled operation
OUTCOME_CONFIRMED = "CONFIRMED"           # reported to the API/UI
OUTCOME_PENDING = "PENDING_CONFIRMATION"
PENDING_MESSAGE = ("Submitted to the blockchain but not yet confirmed. The record is saved and will be "
                   "completed automatically once the ledger confirms it. Do not submit it again.")


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


def _need_identity(user):
    if not user.fabric_identity:
        raise LedgerError("Your account has no Fabric identity configured. Contact the administrator.", 409)


# ------------------------------------------------- write-ahead operations (saga)
def open_ops(bag_pk):
    """Operations whose ledger outcome has not been settled yet."""
    return (BlockchainTransaction.query.filter_by(bag_pk=bag_pk, status=PENDING)
            .order_by(BlockchainTransaction.id).all())


def _begin_op(user, bag_pk, function, request_id):
    op = BlockchainTransaction(bag_pk=bag_pk, function=function, fabric_identity=user.fabric_identity,
                               user_id=user.id, status=PENDING, request_id=request_id)
    db.session.add(op)
    db.session.flush()
    return op


def _ensure_reconciliation(bag):
    """Create the reconciliation row for a bag whose receipt is CONFIRMED on the ledger.

    Idempotent: never creates a second row and never changes an existing one (the original
    weights and the tolerance that applied at the time are preserved). Also repairs a bag that
    is stuck in RECEIVED/IN_TRANSIT although its reconciliation already exists. Caller commits."""
    recon = ReconciliationRecord.query.filter_by(bag_pk=bag.id).first()
    if recon is not None:
        if bag.status in (STATUS_IN_TRANSIT, STATUS_RECEIVED):
            bag.status = recon.status
        return recon
    receipt = ReceiptRecord.query.filter_by(bag_pk=bag.id).first()
    dispatch = DispatchRecord.query.filter_by(bag_pk=bag.id).first()
    if not receipt or receipt.tx_id is None or not dispatch or dispatch.tx_id is None:
        return None                                        # nothing confirmed yet: cannot reconcile
    tolerance = get_tolerance()
    diff, result = reconcile_weights(dispatch.weight_kg, receipt.weight_kg, tolerance)
    recon = ReconciliationRecord(bag_pk=bag.id, dispatch_weight_kg=dispatch.weight_kg,
                                 receipt_weight_kg=receipt.weight_kg, difference_kg=diff,
                                 tolerance_kg=tolerance, status=result, chain_status="PENDING")
    db.session.add(recon)
    bag.status = result
    actor = db.session.get(User, receipt.operator_id)
    audit.log("BAG_RECONCILED", actor, "bag", bag.bag_id,
              {"result": result, "difference_kg": str(diff), "tolerance_kg": str(tolerance)})
    return recon


def _apply_commit(op, bag, tx_id, block):
    """The ledger confirmed this operation: make its local effect real. Caller commits."""
    op.status, op.tx_id, op.block_number, op.error = "COMMITTED", tx_id, block, None
    actor = db.session.get(User, op.user_id)
    if op.function == "RegisterBag":
        bag.register_tx_id = tx_id
        audit.log("BAG_REGISTERED", actor, "bag", bag.bag_id,
                  {"mls": bag.mls.code, "fps": bag.fps.code, "tx": tx_id})
    elif op.function == "DispatchBag":
        rec = DispatchRecord.query.filter_by(bag_pk=bag.id).one()
        rec.tx_id = tx_id
        bag.status = STATUS_IN_TRANSIT
        audit.log("BAG_DISPATCHED", actor, "bag", bag.bag_id, {"tx": tx_id, "mls": bag.mls.code})
    elif op.function == "ReceiveBag":
        rec = ReceiptRecord.query.filter_by(bag_pk=bag.id).one()
        rec.tx_id = tx_id
        audit.log("BAG_RECEIVED", actor, "bag", bag.bag_id, {"tx": tx_id, "fps": bag.fps.code})
        _ensure_reconciliation(bag)                       # receipt + reconciliation in the SAME commit
    elif op.function == "RecordReconciliation":
        recon = ReconciliationRecord.query.filter_by(bag_pk=bag.id).one()
        recon.chain_status, recon.chain_tx_id = "CONFIRMED", tx_id


def _apply_failure(op, bag, error):
    """The operation is certainly NOT on the ledger: remove the intended local record. Caller commits."""
    op.status, op.error = "FAILED", (error or "")[:2000]
    actor = db.session.get(User, op.user_id)
    if op.function == "RegisterBag":
        op.bag_pk = None                                   # keep the failed attempt visible, drop the bag row
        db.session.flush()
        db.session.delete(bag)
    elif op.function == "DispatchBag":
        DispatchRecord.query.filter_by(bag_pk=bag.id, tx_id=None).delete()
    elif op.function == "ReceiveBag":
        ReceiptRecord.query.filter_by(bag_pk=bag.id, tx_id=None).delete()
    audit.log("CHAIN_TX_FAILED", actor, "bag", bag.bag_id, {"function": op.function, "error": (error or "")[:300]})


def _settle(op_id, res: TxResult, bag_pk=None):
    """Apply a VERIFIED outcome to the PENDING operation `op_id`. Idempotent and race-safe:
    returns the operation's final status ('COMMITTED' | 'FAILED' | 'PENDING')."""
    # Lock order is always bag -> operation row -> audit head, the same as every other write path.
    target_pk = db.session.query(BlockchainTransaction.bag_pk).filter_by(id=op_id).scalar() or bag_pk
    bag = Bag.query.filter_by(id=target_pk).with_for_update().one()
    op = (BlockchainTransaction.query.filter_by(id=op_id).with_for_update()
          .populate_existing().one())
    if op.status != PENDING:
        db.session.rollback()                              # release the locks
        return op.status                                   # already settled by a concurrent resolver
    if res.state == COMMITTED:
        _apply_commit(op, bag, res.tx_id, res.block)
    elif res.state == FAILED:
        _apply_failure(op, bag, res.error)
    else:                                                  # UNKNOWN: keep everything, remember what we know
        if res.tx_id:
            op.tx_id = res.tx_id
        op.error = (res.error or "")[:2000]
        audit.log("CHAIN_OUTCOME_UNKNOWN", db.session.get(User, op.user_id), "bag", bag.bag_id,
                  {"function": op.function, "tx": res.tx_id, "note": "kept pending, not rolled back"})
    status = op.status
    db.session.commit()
    return status


def _chain_state(identity, bag_id):
    """On-chain bag record: dict, {} if the bag is not on the ledger, None if the ledger can't be asked."""
    try:
        return get_ledger().evaluate(identity, "GetBagStatus", [bag_id])["result"] or {}
    except LedgerError as e:
        return {} if "BAG_NOT_FOUND" in e.message else None


def _evidence(op, bag):
    """Ask the ledger what happened to a PENDING operation.
    Returns ('COMMITTED', tx_id, block) | ('FAILED', error) | ('NOT_COMMITTED',) | None (ledger unreachable)."""
    if op.tx_id:
        st = tx_status(op.fabric_identity, op.tx_id)
        if st is None:
            return None
        if st.get("state") == "VALID":
            blk = st.get("blockNumber")
            return ("COMMITTED", op.tx_id, int(blk) if blk is not None else None)
        if st.get("state") == "INVALID":
            return ("FAILED", f"Transaction was rejected at commit ({st.get('code')}).")
    chain = _chain_state(op.fabric_identity, bag.bag_id)   # the transaction id may be unknown (e.g. HTTP timeout)
    if chain is None:
        return None
    if op.function == "RegisterBag" and chain.get("bagId") == bag.bag_id:
        return ("COMMITTED", chain.get("registerTxId"), None)
    if op.function == "DispatchBag":
        rec = DispatchRecord.query.filter_by(bag_pk=bag.id).first()
        if rec and chain.get("dispatchCommitment") == rec.commitment:
            return ("COMMITTED", chain.get("dispatchTxId"), None)
    if op.function == "ReceiveBag":
        rec = ReceiptRecord.query.filter_by(bag_pk=bag.id).first()
        if rec and chain.get("receiptCommitment") == rec.commitment:
            return ("COMMITTED", chain.get("receiptTxId"), None)
    if op.function == "RecordReconciliation":
        recon = ReconciliationRecord.query.filter_by(bag_pk=bag.id).first()
        onchain = chain.get("reconciliation")
        if recon and onchain and onchain.get("result") == recon.status:
            return ("COMMITTED", onchain.get("txId"), None)
    return ("NOT_COMMITTED",)


def resolve_pending(bag, abandon=False):
    """Settle every unresolved operation of `bag` that the ledger can now answer for.

    * confirmed on the ledger (by tx id or by the on-chain bag state)  -> completed
    * rejected at commit                                                -> undone
    * not on the ledger yet                                             -> stays PENDING, untouched
    * abandon=True (admin only) and older than PENDING_ABANDON_MINUTES and the ledger positively
      does NOT have it                                                  -> undone, audited
    Returns the operations that are still pending."""
    limit = timedelta(minutes=int(current_app.config.get("PENDING_ABANDON_MINUTES", 15)))
    for op in open_ops(bag.id):
        ev = _evidence(op, bag)
        if ev is None:
            continue
        if ev[0] == "COMMITTED":
            _settle(op.id, TxResult(COMMITTED, ev[1], ev[2]), bag.id)
        elif ev[0] == "FAILED":
            _settle(op.id, TxResult(FAILED, op.tx_id, None, ev[1], 409), bag.id)
        elif abandon and utcnow() - op.created_at >= limit:
            _settle(op.id, TxResult(FAILED, op.tx_id, None,
                                    f"Abandoned by an administrator: not on the ledger after {limit}.", 409), bag.id)
    return open_ops(bag.id)


def heal_bag(bag):
    """Best-effort self-repair used when a bag is viewed: settle old pending operations and make sure
    a confirmed receipt has its reconciliation. Never raises; never contacts Fabric unless needed."""
    try:
        if open_ops(bag.id):
            resolve_pending(bag)
            bag = db.session.get(Bag, bag.id)
        if bag.status in (STATUS_IN_TRANSIT, STATUS_RECEIVED) and _ensure_reconciliation(bag) is not None:
            db.session.commit()
    except (LedgerError, IntegrityError):
        db.session.rollback()


def _block_if_pending(bag_id):
    """Before a new write on a bag: settle what can be settled; refuse while something is unresolved.
    Returns the (re-locked) bag."""
    bag = _locked_bag(bag_id)
    if open_ops(bag.id):
        resolve_pending(bag)                               # commits, which releases the row lock
        bag = _locked_bag(bag_id)
        if open_ops(bag.id):
            raise WorkflowError("An earlier submission for this bag is still waiting for blockchain confirmation. "
                                "Please wait; it will complete automatically. Do not submit it again.",
                                409, "PENDING_CONFIRMATION")
    return bag


def _finish(op_id, res, bag_pk):
    """Settle and translate the result for the API: raise on definite failure, else the outcome."""
    status = _settle(op_id, res, bag_pk)
    if status == "FAILED":
        raise LedgerError(res.error or "Blockchain request failed", res.status if res.state == FAILED else 409, FAILED)
    return OUTCOME_CONFIRMED if status == "COMMITTED" else OUTCOME_PENDING


# ------------------------------------------------------------------- register
def next_bag_id() -> str:
    n = 0
    for (bid,) in db.session.query(Bag.bag_id).all():
        m = re.match(r"^BAG-(\d+)$", bid)
        if m:
            n = max(n, int(m.group(1)))
    return f"BAG-{n + 1:04d}"


def register_bag(admin, mls_id, fps_id, bag_id=None, commodity="Rice", consignment_code=None):
    """Returns the bag. `bag.register_tx_id` is None while the registration awaits confirmation."""
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
    _need_identity(admin)

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
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise WorkflowError(f"{bag_id} is already registered", 409, "DUPLICATE_BAG")
    rid = new_request_id()
    op = _begin_op(admin, bag.id, "RegisterBag", rid)
    op_id, bag_pk, identity = op.id, bag.id, admin.fabric_identity
    args = [bag_id, commodity, mls.code, fps.code, cons.code, rid]
    db.session.commit()                                    # write-ahead: the intent is durable before Fabric is called

    res = submit_tx(identity, "RegisterBag", args)
    _finish(op_id, res, bag_pk)
    return db.session.get(Bag, bag_pk)


# ------------------------------------------------------------------- dispatch
def dispatch_bag(user, bag_id, weight_raw):
    """Returns (bag, dispatch_record, outcome) with outcome CONFIRMED or PENDING_CONFIRMATION."""
    _require_operator(user, "mls")
    bag_id = normalize_bag_id(bag_id)
    weight = parse_weight(weight_raw)
    _need_identity(user)
    bag = _locked_bag(bag_id)
    if bag.mls_id != user.mls_location_id:
        raise WorkflowError("This bag does not belong to your MLS location", 403, "WRONG_LOCATION")
    bag = _block_if_pending(bag_id)
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
    op = _begin_op(user, bag.id, "DispatchBag", rid)
    op_id, bag_pk, rec_id, identity = op.id, bag.id, rec.id, user.fabric_identity
    db.session.commit()                                    # write-ahead

    res = submit_tx(identity, "DispatchBag", [bag_id, commit_hash, rid])
    outcome = _finish(op_id, res, bag_pk)
    return db.session.get(Bag, bag_pk), db.session.get(DispatchRecord, rec_id), outcome


# -------------------------------------------------------------------- receive
def receive_bag(user, bag_id, weight_raw):
    """Returns (bag, outcome). The receipt and its reconciliation are saved in ONE commit once the
    ledger confirms the receipt; the dispatch weight is never shown before that."""
    _require_operator(user, "fps")
    bag_id = normalize_bag_id(bag_id)
    weight = parse_weight(weight_raw)
    _need_identity(user)
    bag = _locked_bag(bag_id)
    if bag.fps_id != user.fps_location_id:
        raise WorkflowError("This bag is not assigned to your FPS", 403, "WRONG_LOCATION")
    bag = _block_if_pending(bag_id)
    if db.session.query(ReceiptRecord.id).filter_by(bag_pk=bag.id).first():
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
    op = _begin_op(user, bag.id, "ReceiveBag", rid)
    op_id, bag_pk, identity = op.id, bag.id, user.fabric_identity
    db.session.commit()                                    # write-ahead

    res = submit_tx(identity, "ReceiveBag", [bag_id, commit_hash, rid])
    outcome = _finish(op_id, res, bag_pk)
    if outcome == OUTCOME_CONFIRMED:
        sync_reconciliation_to_chain(user, bag_id)         # best effort; stays PENDING + retryable on failure
    return db.session.get(Bag, bag_pk), outcome


def recover_bag(bag):
    """Admin recovery: settle pending operations and (re)create a missing reconciliation.
    Returns the reconciliation row or None if the receipt is not confirmed on the ledger."""
    if open_ops(bag.id):
        resolve_pending(bag)
    bag = db.session.get(Bag, bag.id)
    recon = _ensure_reconciliation(bag)
    db.session.commit()
    return recon


def sync_reconciliation_to_chain(actor, bag_id) -> bool:
    """Write the reconciliation result to Fabric. Safe to call repeatedly:
    it first asks the ledger whether the result is already there and never resends blindly."""
    bag = Bag.query.filter_by(bag_id=bag_id).one()
    recon = recover_bag(bag)
    if recon is None:
        return False
    if recon.chain_status == "CONFIRMED":
        return True
    bag = db.session.get(Bag, bag.id)
    if any(o.function == "RecordReconciliation" for o in open_ops(bag.id)):
        return False                                       # an earlier attempt is still unresolved: wait for the ledger
    chain = _chain_state(actor.fabric_identity, bag_id)
    onchain = (chain or {}).get("reconciliation")
    if onchain and onchain.get("result") == recon.status:  # already committed earlier (e.g. lost response)
        recon.chain_status, recon.chain_tx_id = "CONFIRMED", onchain.get("txId")
        db.session.commit()
        return True
    if chain is None:
        return False                                       # ledger unreachable: stay PENDING, retry later
    rid = new_request_id()
    op = _begin_op(actor, bag.id, "RecordReconciliation", rid)
    op_id, bag_pk, identity, status = op.id, bag.id, actor.fabric_identity, recon.status
    args = [bag_id, status, fmt_weight(recon.difference_kg), fmt_weight(recon.tolerance_kg), rid]
    db.session.commit()                                    # write-ahead

    res = submit_tx(identity, "RecordReconciliation", args)
    return _settle(op_id, res, bag_pk) == "COMMITTED"


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

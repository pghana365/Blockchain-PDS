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
import hashlib
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (Bag, BlockchainTransaction, Consignment, Correction, DispatchRecord, FpsLocation,
                      MlsLocation, ReceiptRecord, ReconciliationRecord, Setting, User)
from ..models.constants import (COMMODITIES, MAX_BAG_WEIGHT_KG, STATUS_FLAGGED, STATUS_IN_TRANSIT,
                                STATUS_MATCHED, STATUS_RECEIVED, STATUS_REGISTERED, utcnow)
from . import audit
from .ledger import (COMMITTED, FAILED, UNKNOWN, LedgerError, TxResult, chain_submit, commitment, fmt_weight,
                     get_ledger, new_request_id, new_salt, submit_tx, tx_status)
from .settings import TOLERANCE_KEY, get_tolerance
from ..utils.shop_codes import new_consignment_reference, new_ledger_consignment_id

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


def _not_found(bag_id):
    """Same status, code and text for 'does not exist' and 'belongs to another location', so an
    operator cannot probe which bag ids exist elsewhere (and from that, where they go)."""
    return WorkflowError(f"Bag {bag_id} not found or not associated with your location", 404, "BAG_NOT_FOUND")


def _locked_bag(bag_id: str) -> Bag:
    bag = Bag.query.filter_by(bag_id=bag_id).with_for_update().one_or_none()
    if bag is None:
        raise _not_found(bag_id)
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
        cons_id = bag.consignment_id
        db.session.delete(bag)
        db.session.flush()
        # a consignment that never got a confirmed bag must not linger as an orphan in MySQL (it has
        # no ledger counterpart); a consignment that still has other bags is kept untouched
        if not db.session.query(Bag.id).filter_by(consignment_id=cons_id).first():
            cons = db.session.get(Consignment, cons_id)
            if cons is not None:
                db.session.delete(cons)
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
def next_bag_id(locked=False) -> str:
    n = 0
    query = db.session.query(Bag.bag_id)
    if locked:
        query = query.with_for_update()
    for (bid,) in query.all():
        m = re.match(r"^BAG-(\d+)$", bid)
        if m:
            n = max(n, int(m.group(1)))
    return f"BAG-{n + 1:04d}"


def _lock_bag_id_allocator():
    """Serialize automatic bag-id allocation across concurrent database requests."""
    row = Setting.query.filter_by(key=TOLERANCE_KEY).with_for_update().one_or_none()
    if row is None:
        raise WorkflowError("Application settings are not initialized", 500, "SETTINGS_NOT_INITIALIZED")


MAX_CONSIGNMENT_ATTEMPTS = 8


class _IdentifierCollision(Exception):
    """A freshly drawn consignment reference / ledger id already exists (UNIQUE rejected it)."""


def _new_consignment(admin, mls, fps):
    """Insert a consignment with a random reference and an independent random ledger id.
    Uniqueness is decided by the database's UNIQUE indexes, not by a racy pre-check: a duplicate
    aborts the flush and the caller retries the whole preparation in a fresh transaction."""
    cons = Consignment(code=new_ledger_consignment_id(), reference=new_consignment_reference(),
                       mls_id=mls.id, fps_id=fps.id, created_by=admin.id)
    db.session.add(cons)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise _IdentifierCollision() from exc
    return cons


def _existing_consignment(reference, mls, fps):
    """An admin-chosen consignment. Its route is permanent, so the request must name the same shops."""
    cons = Consignment.query.filter_by(reference=reference).one_or_none()
    if cons is None:
        raise WorkflowError("No consignment with that reference", 404, "CONSIGNMENT_NOT_FOUND")
    if cons.mls_id != mls.id or cons.fps_id != fps.id:
        raise WorkflowError(
            f"Consignment {cons.reference} runs from {cons.mls.shop_code} to {cons.fps.shop_code}. "
            "Choose that MLS and FPS, or leave the consignment empty to start a new one.", 409, "CONSIGNMENT_MISMATCH")
    return cons


def _prepare_register(admin, cons, bag_id, commodity):
    """Persist one registration intent without contacting Fabric. Ledger arguments are only ids:
    bag id, commodity (fixed list), ledger location ids, the server-generated consignment id."""
    if Bag.query.filter_by(bag_id=bag_id).first():
        raise WorkflowError(f"{bag_id} is already registered", 409, "DUPLICATE_BAG")
    bag = Bag(bag_id=bag_id, commodity=commodity, consignment_id=cons.id, mls_id=cons.mls_id, fps_id=cons.fps_id,
              status=STATUS_REGISTERED, created_by=admin.id)
    db.session.add(bag)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise WorkflowError(f"{bag_id} is already registered", 409, "DUPLICATE_BAG")
    rid = new_request_id()
    op = _begin_op(admin, bag.id, "RegisterBag", rid)
    return bag, op, [bag_id, commodity, cons.mls.code, cons.fps.code, cons.code, rid]


def _validate_register_request(admin, mls_id, fps_id, commodity):
    if not admin.is_admin:
        raise WorkflowError("Only an administrator can register bags", 403, "FORBIDDEN")
    mls, fps = db.session.get(MlsLocation, mls_id), db.session.get(FpsLocation, fps_id)
    if not mls or not mls.is_active or not fps or not fps.is_active:
        raise WorkflowError("Choose an active MLS and an active destination FPS", 400, "INVALID_LOCATION")
    commodity = (commodity or "Rice").strip()
    if commodity not in COMMODITIES:                       # fixed list: free text would reach the ledger
        raise WorkflowError(f"Commodity must be one of: {', '.join(COMMODITIES)}", 400, "INVALID_INPUT")
    _need_identity(admin)
    return mls, fps, commodity


def _prepare_batch(admin, mls_id, fps_id, commodity, consignment_ref, bag_ids):
    """Validate, choose/create the consignment and write all registration intents in ONE transaction.
    Retried from scratch if a new consignment's random identifiers collide (also under concurrency:
    the loser of a race gets the UNIQUE violation, rolls back everything and draws again).
    `bag_ids` is a list of explicit ids or None per bag (None = next free id). Caller commits."""
    for _ in range(MAX_CONSIGNMENT_ATTEMPTS):
        mls, fps, commodity = _validate_register_request(admin, mls_id, fps_id, commodity)
        if any(b is None for b in bag_ids):
            _lock_bag_id_allocator()
        try:
            cons = (_existing_consignment(consignment_ref, mls, fps) if consignment_ref
                    else _new_consignment(admin, mls, fps))
        except _IdentifierCollision:
            continue
        return [_prepare_register(admin, cons, b or next_bag_id(locked=True), commodity) for b in bag_ids]
    raise WorkflowError("Could not allocate a unique consignment reference; please retry", 503, "RETRY")


def register_bag(admin, mls_id, fps_id, bag_id=None, commodity="Rice", consignment_ref=None):
    """Returns the bag. `bag.register_tx_id` is None while the registration awaits confirmation.
    consignment_ref: reference of an existing consignment, or None for a new consignment."""
    if bag_id:
        bag_id = normalize_bag_id(bag_id)
    ((bag, op, args),) = _prepare_batch(admin, mls_id, fps_id, commodity, consignment_ref, [bag_id])
    op_id, bag_pk, identity = op.id, bag.id, admin.fabric_identity
    db.session.commit()                                    # write-ahead: the intent is durable before Fabric is called

    res = submit_tx(identity, "RegisterBag", args)
    _finish(op_id, res, bag_pk)
    return db.session.get(Bag, bag_pk)


def register_bags(admin, mls_id, fps_id, count, commodity="Rice", consignment_ref=None):
    """Register independent bags concurrently without bypassing write-ahead recovery.
    All bags of one call share one consignment (new, or the chosen existing one)."""
    if count < 1:
        raise WorkflowError("count must be at least 1", 400, "INVALID_COUNT")
    prepared = _prepare_batch(admin, mls_id, fps_id, commodity, consignment_ref, [None] * count)
    db.session.commit()

    from concurrent.futures import ThreadPoolExecutor

    identity = admin.fabric_identity
    app = current_app._get_current_object()

    def submit_register(args):
        with app.app_context():
            return submit_tx(identity, "RegisterBag", args)

    with ThreadPoolExecutor(max_workers=min(count, 8)) as executor:
        futures = [executor.submit(submit_register, args)
                   for _, _, args in prepared]
        results = [future.result() for future in futures]

    first_error = None
    pending = False
    for (bag, op, _), result in zip(prepared, results):
        status = _settle(op.id, result, bag.id)
        if status == "FAILED" and first_error is None:
            first_error = LedgerError(result.error or "Blockchain request failed", result.status, FAILED)
        pending = pending or status == PENDING
    if first_error:
        raise first_error
    return ([db.session.get(Bag, bag.id) for bag, _, _ in prepared],
            OUTCOME_PENDING if pending else OUTCOME_CONFIRMED)


# ------------------------------------------------------------------- dispatch
def dispatch_bag(user, bag_id, weight_raw):
    """Returns (bag, dispatch_record, outcome) with outcome CONFIRMED or PENDING_CONFIRMATION."""
    _require_operator(user, "mls")
    bag_id = normalize_bag_id(bag_id)
    weight = parse_weight(weight_raw)
    _need_identity(user)
    bag = _locked_bag(bag_id)
    if bag.mls_id != user.mls_location_id:
        raise _not_found(bag_id)       # same answer as a non-existent bag: no existence oracle
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
        raise _not_found(bag_id)
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
def correction_commitment(bag_id, note, salt):
    """What the ledger stores for a correction: 'sha256:' + hex(sha256(bag_id|note|salt)). 71 chars,
    within the chaincode's 5-500 limit, so the chaincode is unchanged."""
    return "sha256:" + hashlib.sha256(f"{bag_id}|{note}|{salt}".encode("utf-8")).hexdigest()


def add_correction(admin, bag_id, note):
    if not admin.is_admin:
        raise WorkflowError("Only an administrator can record corrections", 403, "FORBIDDEN")
    note = (note or "").strip()
    if not 5 <= len(note) <= 500:
        raise WorkflowError("Correction note must be 5-500 characters", 400, "INVALID_INPUT")
    bag = _locked_bag(normalize_bag_id(bag_id))
    rid = new_request_id()
    # The note is free text and may name a shop or place, so the ledger only receives a salted
    # commitment that anchors it; the readable note stays in MySQL (admin/auditor only).
    salt = secrets.token_hex(16)
    out = chain_submit(admin, bag, "RecordCorrection", [bag.bag_id, correction_commitment(bag.bag_id, note, salt), rid], rid)
    c = Correction(bag_pk=bag.id, note=note, note_salt=salt, admin_id=admin.id, tx_id=out["txId"])
    db.session.add(c)
    audit.log("CORRECTION_RECORDED", admin, "bag", bag.bag_id, {"note": note, "tx": out["txId"]})
    db.session.commit()
    return c

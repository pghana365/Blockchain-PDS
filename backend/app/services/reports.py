from sqlalchemy import func

from ..extensions import db
from ..models import (Bag, BlockchainTransaction, Consignment, DispatchRecord, ReceiptRecord,
                      ReconciliationRecord, Role, User)
from ..models.constants import (ROLE_FPS, ROLE_MLS, STATUS_FLAGGED, STATUS_IN_TRANSIT, STATUS_MATCHED)
from . import access


def admin_stats():
    count = lambda q: db.session.execute(q).scalar_one()  # noqa: E731
    operators = dict(db.session.execute(
        db.select(Role.name, func.count(User.id)).join(User, User.role_id == Role.id)
        .where(User.is_enabled.is_(True)).group_by(Role.name)).all())
    return {
        "total_bags": count(db.select(func.count(Bag.id))),
        "dispatched": count(db.select(func.count(DispatchRecord.id))),
        "received": count(db.select(func.count(ReceiptRecord.id))),
        "matched": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.status == STATUS_MATCHED)),
        "flagged": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.status == STATUS_FLAGGED)),
        "pending_deliveries": count(db.select(func.count(Bag.id)).where(Bag.status == STATUS_IN_TRANSIT)),
        "mls_operators": operators.get(ROLE_MLS, 0),
        "fps_operators": operators.get(ROLE_FPS, 0),
        "chain_sync_pending": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.chain_status != "CONFIRMED")),
    }


def mls_rows(user, status=None):
    """MLS operator's own bags with THEIR dispatch weight (allowed)."""
    q = (db.session.query(Bag, DispatchRecord)
         .outerjoin(DispatchRecord, DispatchRecord.bag_pk == Bag.id)
         .filter(Bag.mls_id == user.mls_location_id))
    if status:
        q = q.filter(Bag.status == status)
    return q.order_by(Bag.id.desc()).limit(500).all()


def fps_incoming(user):
    """Bags in transit to this FPS. NO weight tables are joined."""
    return (Bag.query.filter(Bag.fps_id == user.fps_location_id, Bag.status == STATUS_IN_TRANSIT)
            .order_by(Bag.id.desc()).limit(500).all())


def fps_receipts(user):
    """Only bags this FPS has already received; joins are legal because a receipt exists."""
    return (db.session.query(Bag, ReceiptRecord, ReconciliationRecord)
            .join(ReceiptRecord, ReceiptRecord.bag_pk == Bag.id)
            .outerjoin(ReconciliationRecord, ReconciliationRecord.bag_pk == Bag.id)
            .filter(Bag.fps_id == user.fps_location_id)
            .order_by(ReceiptRecord.id.desc()).limit(500).all())


def audit_rows(q=None, status=None, limit=500):
    """Admin/auditor full listing."""
    query = (db.session.query(Bag, DispatchRecord, ReceiptRecord, ReconciliationRecord)
             .outerjoin(DispatchRecord, DispatchRecord.bag_pk == Bag.id)
             .outerjoin(ReceiptRecord, ReceiptRecord.bag_pk == Bag.id)
             .outerjoin(ReconciliationRecord, ReconciliationRecord.bag_pk == Bag.id))
    if q:
        like = f"%{q.strip().upper()}%"
        query = query.filter(Bag.bag_id.like(like))
    if status:
        query = query.filter(Bag.status == status)
    return query.order_by(Bag.id.desc()).limit(limit).all()


def discrepancy_rows(user, only_flagged=True):
    """Discrepancy report. Never available to FPS before receipt: scoped by role."""
    query = (db.session.query(Bag, ReconciliationRecord)
             .join(ReconciliationRecord, ReconciliationRecord.bag_pk == Bag.id))
    if only_flagged:
        query = query.filter(ReconciliationRecord.status == STATUS_FLAGGED)
    if user.is_mls:
        query = query.filter(Bag.mls_id == user.mls_location_id)
    elif user.is_fps:
        query = query.filter(Bag.fps_id == user.fps_location_id)
    elif not (user.is_admin or user.is_auditor):
        query = query.filter(db.false())
    return query.order_by(ReconciliationRecord.difference_kg.desc()).limit(500).all()


def transactions_query(user, bag_q=None, function=None, status=None):
    q = BlockchainTransaction.query
    if user.is_mls:
        q = q.join(Bag, Bag.id == BlockchainTransaction.bag_pk).filter(Bag.mls_id == user.mls_location_id)
    elif user.is_fps:
        q = q.join(Bag, Bag.id == BlockchainTransaction.bag_pk).filter(Bag.fps_id == user.fps_location_id)
    elif not (user.is_admin or user.is_auditor):
        q = q.filter(db.false())
    if bag_q:
        q = q.join(Bag, Bag.id == BlockchainTransaction.bag_pk, isouter=True) if not (user.is_mls or user.is_fps) else q
        q = q.filter(Bag.bag_id.like(f"%{bag_q.strip().upper()}%"))
    if function:
        q = q.filter(BlockchainTransaction.function == function)
    if status:
        q = q.filter(BlockchainTransaction.status == status)
    return q.order_by(BlockchainTransaction.id.desc())

"""Central authorisation rules for bag data.

BLIND VERIFICATION lives here (and in the SQL scoping below), NOT in HTML/JS:
an FPS operator can only obtain the MLS dispatch weight, the difference or the
reconciliation result AFTER their own receipt for that bag exists in the database.

For FPS users the weight rows are not even SELECTed unless the rule allows it:
existence checks use ID-only queries, and the `visible_*` functions are the only
way API code obtains a record containing a weight.
"""
from ..extensions import db
from ..models import Bag, DispatchRecord, ReceiptRecord, ReconciliationRecord


def _exists(model, bag_pk: int) -> bool:
    return db.session.query(model.id).filter(model.bag_pk == bag_pk).first() is not None


def has_dispatch(bag) -> bool:
    return _exists(DispatchRecord, bag.id)


def has_receipt(bag) -> bool:
    return _exists(ReceiptRecord, bag.id)


def can_view_bag(user, bag) -> bool:
    if user.is_admin or user.is_auditor:
        return True
    if user.is_mls:
        return user.mls_location_id is not None and bag.mls_id == user.mls_location_id
    if user.is_fps:
        return user.fps_location_id is not None and bag.fps_id == user.fps_location_id
    return False


def can_view_dispatch_weight(user, bag) -> bool:
    if not can_view_bag(user, bag):
        return False
    if user.is_fps:
        return has_receipt(bag)  # blind rule: only after this FPS submitted its own receipt
    return True


def can_view_receipt_weight(user, bag) -> bool:
    return can_view_bag(user, bag)


def can_view_reconciliation(user, bag) -> bool:
    if not can_view_bag(user, bag):
        return False
    if user.is_fps:
        return has_receipt(bag)
    return True


def visible_dispatch(user, bag):
    if not can_view_dispatch_weight(user, bag):
        return None
    return DispatchRecord.query.filter_by(bag_pk=bag.id).first()


def visible_receipt(user, bag):
    if not can_view_receipt_weight(user, bag):
        return None
    return ReceiptRecord.query.filter_by(bag_pk=bag.id).first()


def visible_reconciliation(user, bag):
    if not can_view_reconciliation(user, bag):
        return None
    return ReconciliationRecord.query.filter_by(bag_pk=bag.id).first()


def bags_query_for(user):
    """Row-level scoping applied in SQL - operators never even load other locations' bags."""
    q = Bag.query
    if user.is_admin or user.is_auditor:
        return q
    if user.is_mls and user.mls_location_id is not None:
        return q.filter(Bag.mls_id == user.mls_location_id)
    if user.is_fps and user.fps_location_id is not None:
        return q.filter(Bag.fps_id == user.fps_location_id)
    return q.filter(db.false())

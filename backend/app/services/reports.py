from datetime import datetime

from sqlalchemy import func, or_

from ..extensions import db
from ..models import (Bag, BlockchainTransaction, Consignment, DispatchRecord, FpsLocation, MlsLocation,
                      ReceiptRecord, ReconciliationRecord, Role, User)
from ..models.constants import (ROLE_FPS, ROLE_MLS, STATUS_FLAGGED, STATUS_IN_TRANSIT, STATUS_MATCHED)
from . import access


DERIVED_CONSIGNMENT_STATUSES = (
    "PENDING",
    "REGISTERED",
    "IN_TRANSIT",
    "RECEIVED",
    "MATCHED",
    "FLAGGED",
)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _shop(viewer, loc, as_of=None):
    if viewer.is_admin or viewer.is_auditor:
        out = {
            "id": loc.id,
            "code": loc.code,
            "shop_code": loc.shop_code,
            "name": loc.name,
            "mandal": loc.mandal,
            "district": loc.district,
            "area": loc.area,
        }
        if hasattr(loc, "village"):
            out["village"] = loc.village
        return out
    if viewer.is_mls and loc.id == viewer.mls_location_id:
        return {
            "id": loc.id,
            "shop_code": loc.shop_code,
            "name": loc.name,
            "mandal": loc.mandal,
            "district": loc.district,
            "area": loc.area,
        }
    if viewer.is_fps and loc.id == viewer.fps_location_id:
        out = {
            "id": loc.id,
            "shop_code": loc.shop_code,
            "name": loc.name,
            "mandal": loc.mandal,
            "district": loc.district,
            "area": loc.area,
        }
        if hasattr(loc, "village"):
            out["village"] = loc.village
        return out
    return None


def consignments_query_for(viewer):
    q = Consignment.query
    if viewer.is_admin or viewer.is_auditor:
        return q
    if viewer.is_mls and viewer.mls_location_id is not None:
        return q.filter(Consignment.mls_id == viewer.mls_location_id)
    if viewer.is_fps and viewer.fps_location_id is not None:
        return q.filter(Consignment.fps_id == viewer.fps_location_id)
    return q.filter(db.false())


def _consignment_bags(consignment):
    return Bag.query.filter_by(consignment_id=consignment.id).order_by(Bag.id.asc()).all()


def consignment_summary(consignment):
    bags = _consignment_bags(consignment)
    total = len(bags)
    dispatched = 0
    received = 0
    matched = 0
    flagged = 0
    dispatch_times = []
    receipt_times = []

    bag_ids = [bag.id for bag in bags]
    dispatch_by_bag = {}
    receipt_by_bag = {}
    reconciliation_by_bag = {}
    if bag_ids:
        dispatch_by_bag = {row.bag_pk: row for row in DispatchRecord.query.filter(
            DispatchRecord.bag_pk.in_(bag_ids)).all()}
        receipt_by_bag = {row.bag_pk: row for row in ReceiptRecord.query.filter(
            ReceiptRecord.bag_pk.in_(bag_ids)).all()}
        reconciliation_by_bag = {row.bag_pk: row for row in ReconciliationRecord.query.filter(
            ReconciliationRecord.bag_pk.in_(bag_ids)).all()}

    for bag in bags:
        dispatch = dispatch_by_bag.get(bag.id)
        if dispatch is not None and dispatch.tx_id:
            dispatched += 1
            dispatch_times.append(dispatch.dispatched_at)

        receipt = receipt_by_bag.get(bag.id)
        if receipt is not None and receipt.tx_id:
            received += 1
            receipt_times.append(receipt.received_at)

        reconciliation = reconciliation_by_bag.get(bag.id)
        if reconciliation is not None:
            if reconciliation.status == STATUS_MATCHED:
                matched += 1
            elif reconciliation.status == STATUS_FLAGGED:
                flagged += 1

    if total == 0:
        status = "PENDING"
    elif flagged:
        status = "FLAGGED"
    elif received == total:
        status = "MATCHED" if matched == total else ("RECEIVED" if matched == 0 else "RECEIVED")
    elif dispatched or received:
        status = "IN_TRANSIT"
    else:
        status = "REGISTERED"

    return {
        "total": total,
        "dispatched": dispatched,
        "received": received,
        "pending": total - received,
        "matched": matched,
        "flagged": flagged,
        "status": status,
        "dispatch_at": max(dispatch_times) if dispatch_times else None,
        "first_dispatch_at": min(dispatch_times) if dispatch_times else None,
        "latest_receipt_at": max(receipt_times) if receipt_times else None,
    }


def consignments_list(viewer, reference=None, status=None, date_from=None, date_to=None,
                      mls_id=None, fps_id=None):
    q = consignments_query_for(viewer)
    if reference:
        ref_like = f"%{reference.strip().upper()[:40]}%"
        q = q.filter(Consignment.reference.like(ref_like))

    start = _parse_date(date_from)
    end = _parse_date(date_to)
    if start:
        q = q.filter(Consignment.created_at >= start)
    if end:
        q = q.filter(Consignment.created_at < datetime(end.year, end.month, end.day).replace(
            hour=23, minute=59, second=59, microsecond=999999))

    if viewer.is_admin or viewer.is_auditor:
        if mls_id:
            try:
                q = q.filter(Consignment.mls_id == int(mls_id))
            except (TypeError, ValueError):
                q = q.filter(db.false())
        if fps_id:
            try:
                q = q.filter(Consignment.fps_id == int(fps_id))
            except (TypeError, ValueError):
                q = q.filter(db.false())

    consignments = q.order_by(Consignment.created_at.desc(), Consignment.id.desc()).all()
    rows = []
    status_filter = (status or "").strip().upper()
    for consignment in consignments:
        summary = consignment_summary(consignment)
        if status_filter and summary["status"] != status_filter:
            continue
        row = {
            "reference": consignment.reference,
            "ledger_id": consignment.code if (viewer.is_admin or viewer.is_auditor) else None,
            "created_at": consignment.created_at,
            **summary,
        }
        if viewer.is_admin or viewer.is_auditor:
            row["origin"] = _shop(viewer, consignment.mls)
            row["destination"] = _shop(viewer, consignment.fps)
        elif viewer.is_mls:
            row["origin"] = _shop(viewer, consignment.mls)
        elif viewer.is_fps:
            row["destination"] = _shop(viewer, consignment.fps)
        rows.append(row)
    return rows


def consignment_dashboard_summary(viewer):
    rows = consignments_list(viewer)
    return {
        "total": len(rows),
        "in_transit": sum(row["status"] == "IN_TRANSIT" for row in rows),
        "fully_received": sum(row["status"] in ("RECEIVED", "MATCHED", "FLAGGED")
                              and row["received"] == row["total"] and row["total"] > 0 for row in rows),
        "pending": sum(row["pending"] > 0 for row in rows),
        "flagged": sum(row["flagged"] > 0 for row in rows),
    }


def consignment_detail(viewer, reference):
    consignment = consignments_query_for(viewer).filter(
        Consignment.reference == reference.strip().upper()[:40]).one_or_none()
    if consignment is None:
        return None

    summary = consignment_summary(consignment)
    detail = {
        "reference": consignment.reference,
        "created_at": consignment.created_at,
        "summary": summary,
        "bags": [],
    }
    if viewer.is_admin or viewer.is_auditor:
        detail["ledger_id"] = consignment.code
        detail["origin"] = _shop(viewer, consignment.mls)
        detail["destination"] = _shop(viewer, consignment.fps)
    elif viewer.is_mls:
        detail["origin"] = _shop(viewer, consignment.mls)
    elif viewer.is_fps:
        detail["destination"] = _shop(viewer, consignment.fps)

    for bag in _consignment_bags(consignment):
        dispatch = DispatchRecord.query.filter_by(bag_pk=bag.id).first()
        receipt = access.visible_receipt(viewer, bag)
        reconciliation = access.visible_reconciliation(viewer, bag)

        bag_detail = {
            "bag_id": bag.bag_id,
            "status": bag.status,
            "dispatch": None,
            "receipt": None,
            "difference_kg": None,
            "reconciliation_status": reconciliation.status if reconciliation else None,
            "reconciliation_chain_status": reconciliation.chain_status if reconciliation else None,
        }
        dispatch_visible = access.visible_dispatch(viewer, bag)
        if dispatch_visible:
            bag_detail["dispatch"] = {
                "weight_kg": str(dispatch_visible.weight_kg),
                "dispatched_at": dispatch_visible.dispatched_at,
                "tx_id": dispatch_visible.tx_id,
            }
        if receipt:
            bag_detail["receipt"] = {
                "weight_kg": str(receipt.weight_kg),
                "received_at": receipt.received_at,
                "tx_id": receipt.tx_id,
            }
        if reconciliation:
            bag_detail["difference_kg"] = str(reconciliation.difference_kg)
            bag_detail["tolerance_kg"] = str(reconciliation.tolerance_kg)
        detail["bags"].append(bag_detail)
    return detail


def admin_stats():
    count = lambda q: db.session.execute(q).scalar_one()  # noqa: E731
    operators = dict(db.session.execute(
        db.select(Role.name, func.count(User.id)).join(User, User.role_id == Role.id)
        .where(User.is_enabled.is_(True)).group_by(Role.name)).all())
    return {
        "total_bags": count(db.select(func.count(Bag.id))),
        "dispatched": count(db.select(func.count(DispatchRecord.id)).where(DispatchRecord.tx_id.isnot(None))),
        "received": count(db.select(func.count(ReceiptRecord.id)).where(ReceiptRecord.tx_id.isnot(None))),
        "pending_confirmations": count(db.select(func.count(BlockchainTransaction.id))
                                       .where(BlockchainTransaction.status == "PENDING")),
        "matched": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.status == STATUS_MATCHED)),
        "flagged": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.status == STATUS_FLAGGED)),
        "pending_deliveries": count(db.select(func.count(Bag.id)).where(Bag.status == STATUS_IN_TRANSIT)),
        "mls_operators": operators.get(ROLE_MLS, 0),
        "fps_operators": operators.get(ROLE_FPS, 0),
        "chain_sync_pending": count(db.select(func.count(ReconciliationRecord.id)).where(ReconciliationRecord.chain_status != "CONFIRMED")),
    }


def mls_counts(user):
    """Bags per status at the operator's MLS - always over ALL of the location's bags, never the
    filtered list, so the header counters do not change when the table is filtered."""
    rows = (db.session.query(Bag.status, func.count(Bag.id))
            .filter(Bag.mls_id == user.mls_location_id).group_by(Bag.status).all())
    counts = {s: 0 for s in ("REGISTERED", "IN_TRANSIT", "RECEIVED", "MATCHED", "FLAGGED")}
    counts.update(dict(rows))
    return counts


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


def _search(query, q):
    """Admin/auditor search across the protected mapping: bag id, consignment reference,
    ledger consignment id, and either shop's permanent shop code."""
    if not q:
        return query
    like = f"%{q.strip().upper()[:40]}%"
    query = (query.join(Consignment, Consignment.id == Bag.consignment_id)
             .join(MlsLocation, MlsLocation.id == Bag.mls_id).join(FpsLocation, FpsLocation.id == Bag.fps_id))
    return query.filter(or_(Bag.bag_id.like(like), Consignment.reference.like(like), Consignment.code.like(like),
                            MlsLocation.shop_code.like(like), FpsLocation.shop_code.like(like)))


def audit_rows(q=None, status=None, limit=500):
    """Admin/auditor full listing."""
    query = (db.session.query(Bag, DispatchRecord, ReceiptRecord, ReconciliationRecord)
             .outerjoin(DispatchRecord, DispatchRecord.bag_pk == Bag.id)
             .outerjoin(ReceiptRecord, ReceiptRecord.bag_pk == Bag.id)
             .outerjoin(ReconciliationRecord, ReconciliationRecord.bag_pk == Bag.id))
    query = _search(query, q)
    if status:
        query = query.filter(Bag.status == status)
    return query.order_by(Bag.id.desc()).limit(limit).all()


def discrepancy_rows(user, only_flagged=True, q=None):
    """Discrepancy report. Never available to FPS before receipt: scoped by role."""
    query = (db.session.query(Bag, ReconciliationRecord)
             .join(ReconciliationRecord, ReconciliationRecord.bag_pk == Bag.id))
    if user.is_admin or user.is_auditor:
        query = _search(query, q)
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

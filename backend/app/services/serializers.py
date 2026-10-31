"""Builds API/page views of a bag for a specific user.

Weights, differences and results are only added when services.access allows
it for THAT user and THAT bag - so an FPS operator's view of an unreceived
bag physically contains no weight data.
"""
from ..models import BlockchainTransaction, Correction
from ..models.constants import STATUS_FLAGGED, STATUS_IN_TRANSIT, STATUS_REGISTERED
from . import access
from .ledger import fmt_weight
from .workflow import FLAG_MESSAGE, MATCH_MESSAGE


def iso(dt):
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def bag_view(user, bag, with_transactions=True):
    has_dispatch = access.has_dispatch(bag)
    has_receipt = access.has_receipt(bag)
    v = {
        "bag_id": bag.bag_id,
        "commodity": bag.commodity,
        "consignment_id": bag.consignment.code,
        "origin_mls": {"code": bag.mls.code, "name": bag.mls.name},
        "destination_fps": {"code": bag.fps.code, "name": bag.fps.name},
        "status": bag.status,
        "registered_at": iso(bag.created_at),
        "register_tx_id": bag.register_tx_id,
        # non-sensitive progress flags (safe for every authorised viewer)
        "dispatched": has_dispatch,
        "received": has_receipt,
        "can_dispatch": bool(user.is_mls and bag.mls_id == user.mls_location_id
                             and bag.status == STATUS_REGISTERED and not has_dispatch),
        "can_receive": bool(user.is_fps and bag.fps_id == user.fps_location_id
                            and bag.status == STATUS_IN_TRANSIT and not has_receipt),
    }

    d = access.visible_dispatch(user, bag)
    if d:
        v["dispatch"] = {
            "weight_kg": fmt_weight(d.weight_kg), "dispatched_at": iso(d.dispatched_at),
            "operator": d.operator.full_name, "operator_username": d.operator.username,
            "mls": d.mls.code, "tx_id": d.tx_id, "commitment": d.commitment,
        }
    r = access.visible_receipt(user, bag)
    if r:
        v["receipt"] = {
            "weight_kg": fmt_weight(r.weight_kg), "received_at": iso(r.received_at),
            "operator": r.operator.full_name, "operator_username": r.operator.username,
            "fps": r.fps.code, "tx_id": r.tx_id, "commitment": r.commitment,
        }
    c = access.visible_reconciliation(user, bag)
    if c:
        v["reconciliation"] = {
            "status": c.status, "difference_kg": fmt_weight(c.difference_kg),
            "tolerance_kg": fmt_weight(c.tolerance_kg), "dispatch_weight_kg": fmt_weight(c.dispatch_weight_kg),
            "receipt_weight_kg": fmt_weight(c.receipt_weight_kg), "computed_at": iso(c.computed_at),
            "chain_status": c.chain_status, "chain_tx_id": c.chain_tx_id,
            "message": FLAG_MESSAGE if c.status == STATUS_FLAGGED else MATCH_MESSAGE,
        }

    if with_transactions:
        txs = (BlockchainTransaction.query.filter_by(bag_pk=bag.id, status="COMMITTED")
               .order_by(BlockchainTransaction.id.asc()).all())
        v["transactions"] = [{
            "tx_id": t.tx_id, "function": t.function, "block_number": t.block_number,
            "identity": t.fabric_identity, "operator": t.user.username if t.user else None,
            "at": iso(t.created_at),
        } for t in txs]
        if user.is_admin or user.is_auditor:
            v["corrections"] = [{
                "note": x.note, "by": x.admin.username, "tx_id": x.tx_id, "at": iso(x.created_at)
            } for x in Correction.query.filter_by(bag_pk=bag.id).order_by(Correction.id).all()]
    return v

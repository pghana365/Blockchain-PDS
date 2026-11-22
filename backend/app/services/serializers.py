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


def _shop(loc, level):
    """What a viewer may know about one shop.
    full -> admin/auditor: shop code, internal ledger id, real name and district
    own  -> the operator's own shop: shop code, real name and district
    code -> the other party, MLS viewing the destination: the random shop code only"""
    if level not in ("full", "own"):
        return {"shop_code": loc.shop_code}
    view = {"shop_code": loc.shop_code, "name": loc.name, "mandal": loc.mandal, "district": loc.district,
            "area": loc.area}
    if hasattr(loc, "village"):
        view["village"] = loc.village
    if level == "full":
        view["ledger_id"] = loc.code
    return view


def bag_view(user, bag, with_transactions=True):
    """Everything is an allow-list per role. Fields an operator may not see are never put in
    the dict, so no template, JSON response or browser tool can reveal them."""
    has_dispatch = access.has_dispatch(bag)
    has_receipt = access.has_receipt(bag)
    pending_ops = BlockchainTransaction.query.filter_by(bag_pk=bag.id, status="PENDING").all()
    privileged = bool(user.is_admin or user.is_auditor)
    v = {
        "bag_id": bag.bag_id,
        "commodity": bag.commodity,
        "consignment_ref": bag.consignment.reference,
        "status": bag.status,
        "registered_at": iso(bag.created_at),
        "register_tx_id": bag.register_tx_id,
        # non-sensitive progress flags (safe for every authorised viewer)
        "dispatched": has_dispatch,
        "received": has_receipt,
        "can_dispatch": bool(user.is_mls and bag.mls_id == user.mls_location_id and not pending_ops
                             and bag.status == STATUS_REGISTERED and not has_dispatch),
        "can_receive": bool(user.is_fps and bag.fps_id == user.fps_location_id and not pending_ops
                            and bag.status == STATUS_IN_TRANSIT and not has_receipt),
        # An earlier operation was sent to the blockchain but is not confirmed yet (no weights in here).
        "pending": [{"operation": o.function, "since": iso(o.created_at)} for o in pending_ops],
    }
    if privileged:
        v["consignment_id"] = bag.consignment.code          # ledger id; may encode the route
        v["origin_mls"] = _shop(bag.mls, "full")
        v["destination_fps"] = _shop(bag.fps, "full")
    elif user.is_mls:
        v["origin_mls"] = _shop(bag.mls, "own")
        v["destination_fps"] = _shop(bag.fps, "code")      # destination: random code only
    elif user.is_fps:
        v["destination_fps"] = _shop(bag.fps, "own")       # origin: nothing at all, not even a code

    d = access.visible_dispatch(user, bag)
    if d:
        v["dispatch"] = {"weight_kg": fmt_weight(d.weight_kg), "dispatched_at": iso(d.dispatched_at),
                         "tx_id": d.tx_id, "commitment": d.commitment}
        if privileged or user.is_mls:                        # the dispatcher's identity is MLS-side information
            v["dispatch"].update(operator=d.operator.full_name, operator_username=d.operator.username,
                                 shop=d.mls.shop_code)
    r = access.visible_receipt(user, bag)
    if r:
        v["receipt"] = {"weight_kg": fmt_weight(r.weight_kg), "received_at": iso(r.received_at),
                        "tx_id": r.tx_id, "commitment": r.commitment}
        if privileged or user.is_fps:                        # the receiver's identity is FPS-side information
            v["receipt"].update(operator=r.operator.full_name, operator_username=r.operator.username,
                                shop=r.fps.shop_code)
    c = access.visible_reconciliation(user, bag)
    if c:
        v["reconciliation"] = {
            "status": c.status, "difference_kg": fmt_weight(c.difference_kg),
            "tolerance_kg": fmt_weight(c.tolerance_kg), "dispatch_weight_kg": fmt_weight(c.dispatch_weight_kg),
            "receipt_weight_kg": fmt_weight(c.receipt_weight_kg), "computed_at": iso(c.computed_at),
            "chain_status": c.chain_status, "chain_tx_id": c.chain_tx_id,
            "message": FLAG_MESSAGE if c.status == STATUS_FLAGGED else MATCH_MESSAGE,
        }

    if with_transactions and (user.is_admin or user.is_auditor):
        txs = (BlockchainTransaction.query.filter_by(bag_pk=bag.id, status="COMMITTED")
               .order_by(BlockchainTransaction.id.asc()).all())
        v["transactions"] = [{
            "tx_id": t.tx_id, "function": t.function, "block_number": t.block_number,
            "identity": t.fabric_identity, "operator": t.user.username if t.user else None,
            "at": iso(t.created_at),
        } for t in txs]
        v["corrections"] = [{
            "note": x.note, "by": x.admin.username, "tx_id": x.tx_id, "at": iso(x.created_at)
        } for x in Correction.query.filter_by(bag_pk=bag.id).order_by(Correction.id).all()]
    return v

import io
import re

import qrcode
from flask import Blueprint, current_app, jsonify, send_file, request
from flask_login import current_user, login_required
from qrcode.constants import ERROR_CORRECT_M

from ..middleware.auth import roles_required
from ..models import Bag
from ..models.constants import ROLE_FPS, ROLE_MLS
from ..services import access, reports, workflow
from ..services.serializers import bag_view
from .common import api_guard, json_body

bp = Blueprint("api_ops", __name__, url_prefix="/api")


def _consignment_iso(value):
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _json_consignment_row(row):
    out = {
        "reference": row["reference"],
        "created_at": _consignment_iso(row["created_at"]),
        "status": row["status"],
        "total_bags": row["total"],
        "dispatched_bags": row["dispatched"],
        "received_bags": row["received"],
        "pending_bags": row["pending"],
        "matched_bags": row["matched"],
        "flagged_bags": row["flagged"],
        "first_dispatch_at": _consignment_iso(row["first_dispatch_at"]),
        "latest_dispatch_at": _consignment_iso(row["dispatch_at"]),
        "latest_receipt_at": _consignment_iso(row["latest_receipt_at"]),
    }
    if row.get("ledger_id"):
        out["ledger_id"] = row["ledger_id"]
    if row.get("origin") is not None:
        out["origin"] = row["origin"]
    if row.get("destination") is not None:
        out["destination"] = row["destination"]
    return out


@bp.get("/consignments")
@login_required
@api_guard
def list_consignments():
    rows = reports.consignments_list(
        current_user,
        reference=request.args.get("q"),
        status=request.args.get("status"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        mls_id=request.args.get("mls_id"),
        fps_id=request.args.get("fps_id"),
    )
    return jsonify(
        consignments=[_json_consignment_row(row) for row in rows],
        summary=reports.consignment_dashboard_summary(current_user),
    )


@bp.get("/consignments/<reference>")
@login_required
@api_guard
def get_consignments(reference):
    detail = reports.consignment_detail(current_user, reference)
    if detail is None:
        raise workflow.WorkflowError("Consignment not found or not associated with your location",
                                     404, "CONSIGNMENT_NOT_FOUND")

    summary = dict(detail["summary"])
    summary["dispatch_at"] = _consignment_iso(summary["dispatch_at"])
    summary["first_dispatch_at"] = _consignment_iso(summary["first_dispatch_at"])
    summary["latest_receipt_at"] = _consignment_iso(summary["latest_receipt_at"])
    bags = []
    for bag in detail["bags"]:
        for key in ("dispatch", "receipt"):
            if bag[key]:
                bag[key] = dict(bag[key])
                bag[key]["dispatched_at"] = _consignment_iso(bag[key].get("dispatched_at"))
                bag[key]["received_at"] = _consignment_iso(bag[key].get("received_at"))
        bags.append(bag)
    out = dict(
        reference=detail["reference"],
        created_at=_consignment_iso(detail["created_at"]),
        summary=summary,
        bags=bags,
    )
    if detail.get("ledger_id"):
        out["ledger_id"] = detail["ledger_id"]
    if detail.get("origin") is not None:
        out["origin"] = detail["origin"]
    if detail.get("destination") is not None:
        out["destination"] = detail["destination"]
    return jsonify(out)


def _bag_or_404(bag_id):
    bag_id = workflow.normalize_bag_id(bag_id)
    bag = Bag.query.filter_by(bag_id=bag_id).one_or_none()
    # Unknown bags and bags you may not see look identical (404): no existence oracle.
    if bag is None or not access.can_view_bag(current_user, bag):
        raise workflow.WorkflowError("Bag not found or not associated with your location", 404, "BAG_NOT_FOUND")
    return bag


@bp.get("/bags/<bag_id>")
@login_required
@api_guard
def get_bag(bag_id):
    bag = _bag_or_404(bag_id)
    workflow.heal_bag(bag)          # settle old pending operations / missing reconciliation, if the ledger can answer
    return jsonify(bag_view(current_user, _bag_or_404(bag_id)))


@bp.post("/bags/<bag_id>/dispatch")
@roles_required(ROLE_MLS)
@api_guard
def dispatch(bag_id):
    bag, rec, outcome = workflow.dispatch_bag(current_user, bag_id, json_body().get("weight_kg"))
    view = bag_view(current_user, bag)
    if outcome == workflow.OUTCOME_PENDING:     # saved, but NOT confirmed on the ledger yet: never say "success"
        return jsonify(outcome=outcome, message=workflow.PENDING_MESSAGE, tx_id=rec.tx_id, bag=view), 202
    return jsonify(outcome=outcome, message="Bag dispatched and recorded on the blockchain.",
                   tx_id=rec.tx_id, bag=view), 201


@bp.post("/bags/<bag_id>/receive")
@roles_required(ROLE_FPS)
@api_guard
def receive(bag_id):
    bag, outcome = workflow.receive_bag(current_user, bag_id, json_body().get("weight_kg"))
    view = bag_view(current_user, bag)   # dispatch weight/comparison appear only once the receipt is CONFIRMED
    if outcome == workflow.OUTCOME_PENDING:
        return jsonify(outcome=outcome, message=workflow.PENDING_MESSAGE, tx_id=None, bag=view), 202
    return jsonify(outcome=outcome, message="Receipt recorded and reconciled.",
                   tx_id=view["receipt"]["tx_id"], bag=view), 201


@bp.get("/bags/<bag_id>/qr.png")
@login_required
@api_guard
def qr_png(bag_id):
    """QR payload is ONLY a URL containing the bag id - no weights, no personal data."""
    bag = _bag_or_404(bag_id)
    url = f"{current_app.config['PUBLIC_BASE_URL'].rstrip('/')}/b/{bag.bag_id}"
    img = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=3)
    img.add_data(url)
    img.make(fit=True)
    buf = io.BytesIO()
    img.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png", download_name=f"{bag.bag_id}.png")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.post("/scan/resolve")
@login_required
@api_guard
def resolve_scan():
    """Accepts raw QR text (bag id or /b/<id> URL) and returns the canonical bag page."""
    raw = str(json_body().get("code", ""))[:300]
    m = re.search(r"BAG-\d{4,}", raw.upper())
    if not m:
        raise workflow.WorkflowError("This QR code is not a valid PDS bag code", 400, "INVALID_BAG_ID")
    bag = _bag_or_404(m.group(0))
    return jsonify(bag_id=bag.bag_id, url=f"/b/{bag.bag_id}")

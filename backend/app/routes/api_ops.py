import io
import re

import qrcode
from flask import Blueprint, current_app, jsonify, send_file
from flask_login import current_user, login_required
from qrcode.constants import ERROR_CORRECT_M

from ..middleware.auth import roles_required
from ..models import Bag
from ..models.constants import ROLE_FPS, ROLE_MLS
from ..services import access, workflow
from ..services.serializers import bag_view
from .common import api_guard, json_body

bp = Blueprint("api_ops", __name__, url_prefix="/api")


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

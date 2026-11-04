from flask import Blueprint, jsonify
from flask_login import current_user

from ..extensions import db
from ..middleware.auth import roles_required
from ..models import Bag, FpsLocation, MlsLocation, ReconciliationRecord, Role, User
from ..models.constants import ALL_ROLES, CHAIN_ROLE, ROLE_ADMIN, ROLE_AUDITOR, ROLE_FPS, ROLE_MLS
from ..services import audit, settings, workflow
from ..services.ledger import LedgerError, chain_submit, get_ledger, new_request_id
from ..services.serializers import bag_view
from ..utils import validators as v
from .common import api_guard, json_body

bp = Blueprint("api_admin", __name__, url_prefix="/api/admin")


def default_identity(role, mls, fps):
    if role == ROLE_ADMIN:
        return "pdsadmin"
    if role == ROLE_AUDITOR:
        return "auditor1"
    code = (mls.code if role == ROLE_MLS else fps.code)
    return code.replace("-", "").lower() + "op"


def _check_identity(identity, role, location_code):
    """The Fabric identity must exist in the gateway wallet and match role/location."""
    try:
        info = get_ledger().identity(identity)
    except LedgerError as e:
        raise LedgerError(e.message, e.status)
    if not info.get("exists"):
        loc = location_code or ""
        org = "2" if role in (ROLE_FPS, ROLE_AUDITOR) else "1"
        raise v.ValidationError(
            f"Fabric identity '{identity}' is not enrolled. Run in Ubuntu: "
            f"bash fabric/scripts/enroll-identity.sh {org} {identity} {CHAIN_ROLE[role]} {loc}".strip())
    if info.get("role") != CHAIN_ROLE[role] or (location_code and info.get("location") != location_code):
        raise v.ValidationError(
            f"Fabric identity '{identity}' has role/location {info.get('role')}/{info.get('location')}, "
            f"which does not match this user ({CHAIN_ROLE[role]}/{location_code or '-'}).")


def _resolve_scope(role, data, existing=None):
    """Role + location consistency (an operator is tied to exactly one location)."""
    mls = fps = None
    if role == ROLE_MLS:
        mls = db.session.get(MlsLocation, int(data.get("mls_location_id") or 0))
        if not mls or not mls.is_active:
            raise v.ValidationError("MLS operators need an active MLS location")
    elif role == ROLE_FPS:
        fps = db.session.get(FpsLocation, int(data.get("fps_location_id") or 0))
        if not fps or not fps.is_active:
            raise v.ValidationError("FPS operators need an active FPS location")
    return mls, fps


def user_dict(u):
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role_name,
            "location": u.location_label, "fabric_identity": u.fabric_identity, "enabled": u.is_enabled,
            "locked": bool(u.locked_until)}


@bp.post("/users")
@roles_required(ROLE_ADMIN)
@api_guard
def create_user():
    d = json_body()
    username = v.validate_username(d.get("username"))
    full_name = v.clean_text(d.get("full_name"), "Full name", 2, 120)
    password = v.validate_password(d.get("password"))
    role_name = d.get("role")
    if role_name not in ALL_ROLES:
        raise v.ValidationError("Unknown role")
    if User.query.filter_by(username=username).first():
        raise workflow.WorkflowError("That username is already taken", 409, "DUPLICATE_USER")
    try:
        mls, fps = _resolve_scope(role_name, d)
    except (TypeError, ValueError):
        raise v.ValidationError("Invalid location")
    identity = v.validate_fabric_identity(d.get("fabric_identity")) or default_identity(role_name, mls, fps)
    loc_code = mls.code if mls else (fps.code if fps else "")
    _check_identity(identity, role_name, loc_code)

    user = User(username=username, full_name=full_name, role_id=Role.query.filter_by(name=role_name).one().id,
                mls_location_id=mls.id if mls else None, fps_location_id=fps.id if fps else None,
                fabric_identity=identity, is_enabled=True)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    rid = new_request_id()   # record the role grant on the ledger (chaincode GrantRole)
    chain_submit(current_user, None, "GrantRole", [identity, CHAIN_ROLE[role_name], loc_code, rid], rid)
    audit.log("USER_CREATED", current_user, "user", username, {"role": role_name, "location": loc_code, "identity": identity})
    db.session.commit()
    return jsonify(user_dict(user)), 201


@bp.patch("/users/<int:user_id>")
@roles_required(ROLE_ADMIN)
@api_guard
def update_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        raise workflow.WorkflowError("User not found", 404, "USER_NOT_FOUND")
    d = json_body()
    changes = {}
    if "is_enabled" in d:
        if u.id == current_user.id and not d["is_enabled"]:
            raise workflow.WorkflowError("You cannot disable your own account", 409, "SELF_DISABLE")
        u.is_enabled = bool(d["is_enabled"])
        changes["is_enabled"] = u.is_enabled
    if "full_name" in d:
        u.full_name = v.clean_text(d["full_name"], "Full name", 2, 120)
        changes["full_name"] = True
    if d.get("new_password"):
        u.set_password(v.validate_password(d["new_password"]))
        u.failed_logins, u.locked_until = 0, None
        changes["password_reset"] = True
    if d.get("unlock"):
        u.failed_logins, u.locked_until = 0, None
        changes["unlocked"] = True
    if "mls_location_id" in d or "fps_location_id" in d:
        if u.is_mls or u.is_fps:
            mls, fps = _resolve_scope(u.role_name, d)
            loc = (mls or fps)
            identity = v.validate_fabric_identity(d.get("fabric_identity")) or default_identity(u.role_name, mls, fps)
            _check_identity(identity, u.role_name, loc.code)
            u.mls_location_id, u.fps_location_id = (mls.id if mls else None), (fps.id if fps else None)
            u.fabric_identity = identity
            changes["location"] = loc.code
        else:
            raise v.ValidationError("Only operators have a location")
    if not changes:
        raise v.ValidationError("No changes supplied")
    audit.log("USER_UPDATED", current_user, "user", u.username, changes)
    db.session.commit()
    return jsonify(user_dict(u))


@bp.post("/locations")
@roles_required(ROLE_ADMIN)
@api_guard
def create_location():
    d = json_body()
    kind = d.get("kind")
    if kind not in ("MLS", "FPS"):
        raise v.ValidationError("kind must be MLS or FPS")
    model = MlsLocation if kind == "MLS" else FpsLocation
    code = v.validate_location_code(kind, d.get("code"))
    if model.query.filter_by(code=code).first():
        raise workflow.WorkflowError(f"{code} already exists", 409, "DUPLICATE_LOCATION")
    loc = model(code=code, name=v.clean_text(d.get("name"), "Name", 2, 120),
                district=v.clean_text(d.get("district"), "District", 2, 80))
    db.session.add(loc)
    audit.log("LOCATION_CREATED", current_user, kind.lower(), code, {"name": loc.name})
    db.session.commit()
    return jsonify(id=loc.id, code=loc.code, name=loc.name, district=loc.district), 201


@bp.patch("/locations/<kind>/<int:loc_id>")
@roles_required(ROLE_ADMIN)
@api_guard
def update_location(kind, loc_id):
    if kind not in ("MLS", "FPS"):
        raise workflow.WorkflowError("Unknown location type", 404, "NOT_FOUND")
    loc = db.session.get(MlsLocation if kind == "MLS" else FpsLocation, loc_id)
    if not loc:
        raise workflow.WorkflowError("Location not found", 404, "NOT_FOUND")
    d = json_body()
    if "is_active" in d:
        loc.is_active = bool(d["is_active"])
    if "name" in d:
        loc.name = v.clean_text(d["name"], "Name", 2, 120)
    audit.log("LOCATION_UPDATED", current_user, kind.lower(), loc.code, {k: d[k] for k in ("is_active", "name") if k in d})
    db.session.commit()
    return jsonify(id=loc.id, code=loc.code, is_active=loc.is_active)


@bp.post("/bags")
@roles_required(ROLE_ADMIN)
@api_guard
def register_bags():
    d = json_body()
    try:
        mls_id, fps_id = int(d.get("mls_id")), int(d.get("fps_id"))
        count = int(d.get("count") or 1)
    except (TypeError, ValueError):
        raise v.ValidationError("mls_id, fps_id and count must be numbers")
    if not 1 <= count <= 50:
        raise v.ValidationError("count must be between 1 and 50")
    if count > 1 and d.get("bag_id"):
        raise v.ValidationError("Leave Bag ID empty when registering several bags")
    if count > 1:
        bags, outcome = workflow.register_bags(
            current_user, mls_id, fps_id, count, d.get("commodity") or "Rice",
            d.get("consignment_id") or None)
        return jsonify(
            created=[{"bag_id": bag.bag_id, "tx_id": bag.register_tx_id,
                      "pending": bag.register_tx_id is None} for bag in bags],
            outcome=outcome), (202 if outcome == workflow.OUTCOME_PENDING else 201)
    created = []
    bag = workflow.register_bag(current_user, mls_id, fps_id, d.get("bag_id") or None,
                                d.get("commodity") or "Rice", d.get("consignment_id") or None)
    created.append({"bag_id": bag.bag_id, "tx_id": bag.register_tx_id, "pending": bag.register_tx_id is None})
    return jsonify(created=created, outcome="PENDING_CONFIRMATION" if created[0]["pending"] else "CONFIRMED"), (
        202 if created[0]["pending"] else 201)


@bp.put("/settings/tolerance")
@roles_required(ROLE_ADMIN)
@api_guard
def set_tolerance():
    old = settings.get_tolerance()
    try:
        new = settings.set_tolerance(json_body().get("tolerance_kg"), current_user)
    except ValueError as e:
        raise v.ValidationError(str(e))
    audit.log("TOLERANCE_CHANGED", current_user, "setting", settings.TOLERANCE_KEY, {"old": str(old), "new": str(new)})
    db.session.commit()
    return jsonify(tolerance_kg=str(new), note="Applies to future receipts only; past reconciliations keep their original tolerance.")


@bp.post("/bags/<bag_id>/corrections")
@roles_required(ROLE_ADMIN)
@api_guard
def correction(bag_id):
    c = workflow.add_correction(current_user, bag_id, json_body().get("note"))
    return jsonify(message="Correction recorded as a separate audit event; original records are unchanged.",
                   tx_id=c.tx_id), 201


@bp.post("/bags/<bag_id>/retry-chain-sync")
@roles_required(ROLE_ADMIN)
@api_guard
def retry_sync(bag_id):
    """Idempotent recovery. If the receipt is confirmed but its reconciliation row is missing it is
    rebuilt from the original stored weights; the result is then written to (or found on) the ledger.
    Calling it again never creates a second reconciliation or a second ledger transaction."""
    bag_id = workflow.normalize_bag_id(bag_id)
    bag = Bag.query.filter_by(bag_id=bag_id).one_or_none()
    if not bag:
        raise workflow.WorkflowError("Bag not found", 404, "NOT_FOUND")
    if workflow.recover_bag(bag) is None:
        raise workflow.WorkflowError(
            "There is no confirmed receipt to reconcile for this bag (the receipt is missing or still "
            "waiting for blockchain confirmation).", 409, "NOTHING_TO_RECONCILE")
    ok = workflow.sync_reconciliation_to_chain(current_user, bag_id)
    return jsonify(synced=ok, outcome="CONFIRMED" if ok else "PENDING_CONFIRMATION"), (200 if ok else 202)


@bp.post("/bags/<bag_id>/resolve-pending")
@roles_required(ROLE_ADMIN)
@api_guard
def resolve_pending(bag_id):
    """Ask the ledger about operations of unknown outcome and finish or undo them.
    `abandon: true` additionally undoes operations that the ledger positively does not have, but only
    when they are older than PENDING_ABANDON_MINUTES (default 15)."""
    bag_id = workflow.normalize_bag_id(bag_id)
    bag = Bag.query.filter_by(bag_id=bag_id).one_or_none()
    if not bag:
        raise workflow.WorkflowError("Bag not found", 404, "NOT_FOUND")
    abandon = bool(json_body().get("abandon"))
    left = workflow.resolve_pending(bag, abandon=abandon)
    if abandon:
        audit.log("PENDING_ABANDON_REQUESTED", current_user, "bag", bag_id, {"still_pending": len(left)}, commit=True)
    workflow.heal_bag(bag)
    return jsonify(still_pending=[{"function": o.function, "tx_id": o.tx_id, "since": o.created_at.isoformat() + "Z"}
                                  for o in left])

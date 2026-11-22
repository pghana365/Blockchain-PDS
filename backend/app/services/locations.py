"""Shop directory: creation with a random shop code, versioned identity changes, history lookup.

Every change to a shop's real identity (name / village / mandal / district) closes the current
LocationIdentityHistory row and inserts a new one, in the same transaction as the change, while
the location row is locked. Nothing is ever overwritten or deleted, so "what was this shop called
when bag X was registered?" always has an answer.
"""
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import FpsLocation, LocationIdentityHistory, MlsLocation
from ..models.constants import utcnow
from ..utils import validators as v
from ..utils.shop_codes import new_fps_code, new_mls_code
from .workflow import WorkflowError as LocationError   # same JSON/HTML error handling everywhere

MODELS = {"MLS": MlsLocation, "FPS": FpsLocation}
IDENTITY_FIELDS = {"MLS": ("name", "mandal", "district"), "FPS": ("name", "village", "mandal", "district")}
MAX_CODE_ATTEMPTS = 10


def model_for(kind):
    if kind not in MODELS:
        raise LocationError("Unknown location type", 404, "NOT_FOUND")
    return MODELS[kind]


def kind_of(loc):
    return "MLS" if isinstance(loc, MlsLocation) else "FPS"


def _clean_identity(kind, data, partial=False):
    out = {}
    rules = {"name": ("Name", 2, 120), "mandal": ("Mandal", 2, 80), "district": ("District", 2, 80)}
    for key, (label, lo, hi) in rules.items():
        if key in data or not partial:
            out[key] = v.clean_text(data.get(key), label, lo, hi)
    if kind == "FPS" and ("village" in data or not partial):
        raw = (data.get("village") or "").strip()
        out["village"] = v.clean_text(raw, "Village / area", 2, 120) if raw else None
    return out


def _clean_reason(reason):
    return v.clean_text(reason, "Reason for the change", 5, 300)


def add_version(loc, user, reason, at=None):
    """Insert the current identity of `loc` as a new open version. Caller commits."""
    kind = kind_of(loc)
    row = LocationIdentityHistory(
        kind=kind, location_id=loc.id, shop_code=loc.shop_code, ledger_id=loc.code, name=loc.name,
        village=getattr(loc, "village", None), mandal=loc.mandal, district=loc.district,
        valid_from=at or utcnow(), valid_to=None, changed_by=user.id if user else None, reason=reason)
    db.session.add(row)
    return row


def current_version(kind, loc_id):
    return (LocationIdentityHistory.query.filter_by(kind=kind, location_id=loc_id, valid_to=None)
            .order_by(LocationIdentityHistory.valid_from.desc()).first())


def create_location(kind, ledger_code, data, user, reason="Location registered"):
    """New shop with a server-generated, unique, permanent shop code and its first history version.
    Collisions on the random code are retried; a duplicate ledger id is a clean 409."""
    model = model_for(kind)
    code = v.validate_location_code(kind, ledger_code)
    identity = _clean_identity(kind, data)
    new_code = new_mls_code if kind == "MLS" else new_fps_code
    for _ in range(MAX_CODE_ATTEMPTS):
        if model.query.filter_by(code=code).first():
            raise LocationError(f"{code} already exists", 409, "DUPLICATE_LOCATION")
        loc = model(code=code, shop_code=new_code(), **identity)
        db.session.add(loc)
        try:
            db.session.flush()                      # UNIQUE(shop_code) / UNIQUE(code) decide
        except IntegrityError:
            db.session.rollback()
            if model.query.filter_by(code=code).first():
                raise LocationError(f"{code} already exists", 409, "DUPLICATE_LOCATION")
            continue                                # random shop-code collision: draw again
        add_version(loc, user, reason, at=loc.created_at)
        return loc
    raise LocationError("Could not allocate a unique shop code; please retry", 503, "RETRY")


def update_location(kind, loc_id, data, user):
    """Apply an admin change. Identity changes need a reason and create a new history version.
    is_active is operational state, not identity: audited by the caller, not versioned.
    Returns (location, changed_identity_fields)."""
    model = model_for(kind)
    loc = model.query.filter_by(id=loc_id).with_for_update().one_or_none()   # serialise concurrent edits
    if loc is None:
        raise LocationError("Location not found", 404, "NOT_FOUND")
    forbidden = {"shop_code", "code", "ledger_id", "id"} & set(data)
    if forbidden:
        raise LocationError(f"{', '.join(sorted(forbidden))} cannot be changed: shop codes and ledger ids are permanent",
                            409, "IMMUTABLE")
    identity = _clean_identity(kind, {k: data[k] for k in IDENTITY_FIELDS[kind] if k in data}, partial=True)
    changed = {k: val for k, val in identity.items() if getattr(loc, k) != val}
    if changed:
        reason = _clean_reason(data.get("reason"))
        now = utcnow()
        cur = current_version(kind, loc.id)
        if cur is not None:
            if cur.valid_from >= now:                # clock granularity: keep versions strictly ordered
                from datetime import timedelta
                now = cur.valid_from + timedelta(microseconds=1)
            cur.valid_to = now
        for k, val in changed.items():
            setattr(loc, k, val)
        add_version(loc, user, reason, at=now)
    if "is_active" in data:
        loc.is_active = bool(data["is_active"])
    return loc, changed


def history(kind, loc_id):
    return (LocationIdentityHistory.query.filter_by(kind=kind, location_id=loc_id)
            .order_by(LocationIdentityHistory.valid_from.asc(), LocationIdentityHistory.id.asc()).all())


def identity_at(loc, when):
    """The version of `loc` that was valid at `when` (e.g. a bag's registration time).
    Falls back to the earliest recorded version for moments before history began."""
    kind = kind_of(loc)
    q = LocationIdentityHistory.query.filter_by(kind=kind, location_id=loc.id)
    row = (q.filter(LocationIdentityHistory.valid_from <= when)
           .filter((LocationIdentityHistory.valid_to.is_(None)) | (LocationIdentityHistory.valid_to > when))
           .order_by(LocationIdentityHistory.valid_from.desc()).first())
    return row or q.order_by(LocationIdentityHistory.valid_from.asc()).first()


def version_view(row, include_changer=True):
    out = {"name": row.name, "village": row.village, "mandal": row.mandal, "district": row.district,
           "area": row.area, "valid_from": row.valid_from, "valid_to": row.valid_to, "reason": row.reason}
    if include_changer:
        out["changed_by"] = row.changer.username if row.changer else "system"
    return out

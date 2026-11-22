"""Application-level integrity guards, installed on every SQLAlchemy session.

G1  Shop codes and ledger ids never change once a location exists.
G5  A bag's consignment / MLS / FPS and a consignment's id / reference / MLS / FPS never change.
G4  location_identity_history is append-only: rows are inserted; the only permitted change is
    closing the current version (valid_to: NULL -> a timestamp); rows are never deleted.
    A shop's name / village / mandal / district can only change in the same flush that inserts
    the history version recording the new values, so identity can never be overwritten silently.

Enforced in two places so no ORM code path can bypass them:
  * before_flush   - changes to loaded objects (attribute and relationship assignments, deletes)
  * do_orm_execute - bulk UPDATE / DELETE statements run through the session

Raw SQL outside the ORM (e.g. a mysql shell) is not covered here; that would need database
triggers, which this MySQL server only allows with elevated privileges (see docs/architecture.md).
"""
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, attributes

from .bag import Bag, Consignment
from .location import FpsLocation, LocationIdentityHistory, MlsLocation


class GuardViolation(Exception):
    """A write that would rewrite an identifier, a route or history. Never partially applied."""

    status = 409
    code = "IMMUTABLE"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# model -> columns that are fixed once the row exists
IMMUTABLE_COLUMNS = {
    MlsLocation: ("code", "shop_code"),
    FpsLocation: ("code", "shop_code"),
    Consignment: ("code", "reference", "mls_id", "fps_id"),
    Bag: ("bag_id", "consignment_id", "mls_id", "fps_id"),
}
# model -> relationships that would rewrite those columns at flush time
IMMUTABLE_RELATIONSHIPS = {
    Consignment: ("mls", "fps"),
    Bag: ("consignment", "mls", "fps"),
}
HISTORY_COLUMNS = tuple(c.key for c in LocationIdentityHistory.__table__.columns if c.key not in ("id", "valid_to"))

_GUARDED_TABLES = {m.__table__.name: cols for m, cols in IMMUTABLE_COLUMNS.items()}
_HISTORY_TABLE = LocationIdentityHistory.__table__.name


def _changed(obj, key):
    """(old, new) if `key` really changed on a persistent object, else None."""
    hist = attributes.get_history(obj, key)
    if not hist.added:
        return None
    old = hist.deleted[0] if hist.deleted else (hist.unchanged[0] if hist.unchanged else None)
    new = hist.added[0]
    return None if old == new else (old, new)


def _changed_relationship(obj, key):
    hist = attributes.get_history(obj, key)
    if not hist.added:
        return False
    old = hist.deleted[0] if hist.deleted else None
    return hist.added[0] is not old


IDENTITY_COLUMNS = {MlsLocation: ("name", "mandal", "district"), FpsLocation: ("name", "village", "mandal", "district")}


def _has_matching_version(session, loc):
    """True if this flush also inserts the history version that records loc's new identity."""
    kind = "MLS" if isinstance(loc, MlsLocation) else "FPS"
    for new in session.new:
        if (isinstance(new, LocationIdentityHistory) and new.kind == kind and new.location_id == loc.id
                and new.valid_to is None
                and all(getattr(new, k) == getattr(loc, k) for k in IDENTITY_COLUMNS[type(loc)])):
            return True
    return False


@event.listens_for(Session, "before_flush")
def _guard_flush(session, flush_context, instances):
    for obj in session.dirty:
        if not inspect(obj).persistent:
            continue
        model = type(obj)
        if model in IDENTITY_COLUMNS and any(_changed(obj, k) is not None for k in IDENTITY_COLUMNS[model]):
            if not _has_matching_version(session, obj):           # G4: no silent overwrite of identity
                raise GuardViolation("A shop's identity can only change through a recorded history version "
                                     "(use the location update with a reason).")
        for key in IMMUTABLE_COLUMNS.get(model, ()):
            change = _changed(obj, key)
            if change is not None:
                raise GuardViolation(f"{model.__name__}.{key} is permanent and cannot be changed "
                                     f"(attempted {change[0]!r} -> {change[1]!r}).")
        for key in IMMUTABLE_RELATIONSHIPS.get(model, ()):
            if _changed_relationship(obj, key):
                raise GuardViolation(f"{model.__name__}.{key} is an established route and cannot be changed.")
        if model is LocationIdentityHistory:
            for key in HISTORY_COLUMNS:
                if _changed(obj, key) is not None:
                    raise GuardViolation(f"Location history is append-only: {key} cannot be changed.")
            change = _changed(obj, "valid_to")
            if change is not None and not (change[0] is None and change[1] is not None):
                raise GuardViolation("Location history is append-only: a closed version cannot be reopened "
                                     "or re-dated.")
    for obj in session.deleted:
        if isinstance(obj, LocationIdentityHistory):
            raise GuardViolation("Location history is append-only: versions cannot be deleted.")


def _bulk_keys(stmt):
    keys = set()
    for source in (getattr(stmt, "_values", None), getattr(stmt, "_ordered_values", None)):
        if not source:
            continue
        items = source.items() if hasattr(source, "items") else source
        for k, _ in items:
            keys.add(getattr(k, "key", None) or getattr(k, "name", None) or str(k))
    for row in getattr(stmt, "_multi_values", None) or ():
        for values in row:
            keys.update(getattr(k, "key", str(k)) for k in (values.keys() if hasattr(values, "keys") else ()))
    return keys


@event.listens_for(Session, "do_orm_execute")
def _guard_bulk(state):
    if not (state.is_update or state.is_delete):
        return
    table = getattr(getattr(state.statement, "table", None), "name", None)
    if table == _HISTORY_TABLE:
        raise GuardViolation("Location history is append-only: bulk UPDATE/DELETE is not allowed.")
    if state.is_update and table in _GUARDED_TABLES:
        touched = _bulk_keys(state.statement) & set(_GUARDED_TABLES[table])
        if touched:
            raise GuardViolation(f"{table}: {', '.join(sorted(touched))} are permanent and cannot be changed.")

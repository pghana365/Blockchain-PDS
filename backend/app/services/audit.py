import hashlib
import json

from flask import has_request_context, request

from ..extensions import db
from ..models import AuditLog
from ..models.constants import utcnow

GENESIS = "0" * 64


def _payload(username, action, entity_type, entity_id, detail, created_at, ip) -> str:
    return json.dumps(
        [username, action, entity_type, entity_id, detail, created_at.isoformat(), ip],
        separators=(",", ":"), ensure_ascii=True,
    )


def log(action, user=None, entity_type=None, entity_id=None, detail=None, commit=False, username=None):
    """Append an audit entry. Each entry is chained to the previous one by hash."""
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, default=str, separators=(",", ":"))
    ip = request.remote_addr if has_request_context() else None
    uname = username or (user.username if user is not None and getattr(user, "is_authenticated", True) else None)
    created = utcnow().replace(microsecond=0)

    last = AuditLog.query.order_by(AuditLog.id.desc()).first()
    prev = last.entry_hash if last else GENESIS
    entry_hash = hashlib.sha256((prev + _payload(uname, action, entity_type, entity_id, detail, created, ip)).encode()).hexdigest()

    entry = AuditLog(
        user_id=getattr(user, "id", None) if user is not None and getattr(user, "is_authenticated", True) else None,
        username=uname, action=action, entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail, ip_address=ip, created_at=created, prev_hash=prev, entry_hash=entry_hash,
    )
    db.session.add(entry)
    db.session.flush()  # keep chain order within a transaction
    if commit:
        db.session.commit()
    return entry


def verify_chain():
    """Recompute the whole chain. Returns (ok, checked_count, first_bad_id)."""
    prev = GENESIS
    count = 0
    for e in AuditLog.query.order_by(AuditLog.id.asc()).yield_per(500):
        expected = hashlib.sha256(
            (prev + _payload(e.username, e.action, e.entity_type, e.entity_id, e.detail, e.created_at, e.ip_address)).encode()
        ).hexdigest()
        if e.prev_hash != prev or e.entry_hash != expected:
            return False, count, e.id
        prev = e.entry_hash
        count += 1
    return True, count, None

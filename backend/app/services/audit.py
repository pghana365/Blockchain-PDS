import hashlib
import json

from flask import has_request_context, request
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import AuditChainHead, AuditLog
from ..models.constants import utcnow

GENESIS = "0" * 64


def _payload(username, action, entity_type, entity_id, detail, created_at, ip) -> str:
    return json.dumps(
        [username, action, entity_type, entity_id, detail, created_at.isoformat(), ip],
        separators=(",", ":"), ensure_ascii=True,
    )


def _locked_head():
    """Return the chain-head row, locked (SELECT ... FOR UPDATE) until this transaction ends.

    Writers queue here, so each one reads the previous hash only after the writer before it
    has committed. The row is created on first use from the newest existing entry.
    LOCK ORDER: call audit.log() as the LAST thing before commit, after any bag/row locks,
    so a transaction never waits for another row while holding this lock."""
    head = db.session.get(AuditChainHead, 1, with_for_update=True)
    if head is None:
        try:
            with db.session.begin_nested():            # savepoint: a lost race must not abort the outer transaction
                last = AuditLog.query.order_by(AuditLog.id.desc()).first()
                db.session.add(AuditChainHead(id=1, last_hash=last.entry_hash if last else GENESIS))
                db.session.flush()
        except IntegrityError:
            pass                                        # another request created it first
        head = db.session.get(AuditChainHead, 1, with_for_update=True, populate_existing=True)
    return head


def log(action, user=None, entity_type=None, entity_id=None, detail=None, commit=False, username=None):
    """Append an audit entry. Each entry is chained to the previous one by hash.
    Concurrent callers are serialised by the locked chain-head row."""
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, default=str, separators=(",", ":"))
    ip = request.remote_addr if has_request_context() else None
    uname = username or (user.username if user is not None and getattr(user, "is_authenticated", True) else None)
    created = utcnow().replace(microsecond=0)

    head = _locked_head()
    prev = head.last_hash
    entry_hash = hashlib.sha256((prev + _payload(uname, action, entity_type, entity_id, detail, created, ip)).encode()).hexdigest()

    entry = AuditLog(
        user_id=getattr(user, "id", None) if user is not None and getattr(user, "is_authenticated", True) else None,
        username=uname, action=action, entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail, ip_address=ip, created_at=created, prev_hash=prev, entry_hash=entry_hash,
    )
    db.session.add(entry)
    head.last_hash = entry_hash
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

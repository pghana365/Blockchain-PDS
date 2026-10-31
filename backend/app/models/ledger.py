from ..extensions import db
from .constants import utcnow


class BlockchainTransaction(db.Model):
    """Every attempt to write to Hyperledger Fabric (successful or not)."""
    __tablename__ = "blockchain_transactions"

    id = db.Column(db.Integer, primary_key=True)
    tx_id = db.Column(db.String(80), nullable=True, unique=True)  # NULL when failed before submission
    bag_pk = db.Column(db.Integer, db.ForeignKey("bags.id"), nullable=True, index=True)
    function = db.Column(db.String(40), nullable=False)
    fabric_identity = db.Column(db.String(64), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    block_number = db.Column(db.BigInteger, nullable=True)
    status = db.Column(db.String(12), nullable=False, index=True)  # COMMITTED | FAILED
    error = db.Column(db.Text, nullable=True)
    request_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    bag = db.relationship("Bag")
    user = db.relationship("User")


class AuditLog(db.Model):
    """Append-only application audit trail. Entries are hash-chained
    (entry_hash = SHA-256(prev_hash + content)) so edits/deletions are detectable."""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    username = db.Column(db.String(40), nullable=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    entity_type = db.Column(db.String(30), nullable=True)
    entity_id = db.Column(db.String(64), nullable=True, index=True)
    detail = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    prev_hash = db.Column(db.String(64), nullable=False)
    entry_hash = db.Column(db.String(64), nullable=False)

    user = db.relationship("User")


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(200), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

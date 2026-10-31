from ..extensions import db
from .constants import STATUS_REGISTERED, utcnow


class Consignment(db.Model):
    __tablename__ = "consignments"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    mls_id = db.Column(db.Integer, db.ForeignKey("mls_locations.id"), nullable=False, index=True)
    fps_id = db.Column(db.Integer, db.ForeignKey("fps_locations.id"), nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    mls = db.relationship("MlsLocation")
    fps = db.relationship("FpsLocation")


class Bag(db.Model):
    __tablename__ = "bags"

    id = db.Column(db.Integer, primary_key=True)
    bag_id = db.Column(db.String(16), nullable=False, unique=True)  # BAG-0001 (the QR payload)
    commodity = db.Column(db.String(50), nullable=False, default="Rice")
    consignment_id = db.Column(db.Integer, db.ForeignKey("consignments.id"), nullable=False, index=True)
    mls_id = db.Column(db.Integer, db.ForeignKey("mls_locations.id"), nullable=False)
    fps_id = db.Column(db.Integer, db.ForeignKey("fps_locations.id"), nullable=False)
    status = db.Column(db.String(12), nullable=False, default=STATUS_REGISTERED)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    register_tx_id = db.Column(db.String(80), nullable=True)

    consignment = db.relationship("Consignment")
    mls = db.relationship("MlsLocation")
    fps = db.relationship("FpsLocation")
    creator = db.relationship("User", foreign_keys=[created_by])
    # NOTE: deliberately NO relationships to dispatch/receipt/reconciliation rows.
    # Weight data is only reachable via services.access.visible_* so that the
    # blind-verification rule cannot be bypassed by lazy loading in templates.

    __table_args__ = (
        db.Index("ix_bags_mls_status", "mls_id", "status"),
        db.Index("ix_bags_fps_status", "fps_id", "status"),
    )


class DispatchRecord(db.Model):
    """Exactly one per bag (UNIQUE bag_pk) - enforced by the database."""
    __tablename__ = "dispatch_records"

    id = db.Column(db.Integer, primary_key=True)
    bag_pk = db.Column(db.Integer, db.ForeignKey("bags.id"), nullable=False, unique=True)
    weight_kg = db.Column(db.Numeric(8, 3), nullable=False)
    salt = db.Column(db.String(64), nullable=False)          # secret; never leaves the backend/DB
    commitment = db.Column(db.String(64), nullable=False)    # SHA-256(bag|weight|salt) - also on-chain
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    mls_id = db.Column(db.Integer, db.ForeignKey("mls_locations.id"), nullable=False, index=True)
    request_id = db.Column(db.String(64), nullable=False, unique=True)
    tx_id = db.Column(db.String(80), nullable=True)
    dispatched_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bag = db.relationship("Bag")
    operator = db.relationship("User", foreign_keys=[operator_id])
    mls = db.relationship("MlsLocation")


class ReceiptRecord(db.Model):
    """Exactly one per bag (UNIQUE bag_pk) - enforced by the database."""
    __tablename__ = "receipt_records"

    id = db.Column(db.Integer, primary_key=True)
    bag_pk = db.Column(db.Integer, db.ForeignKey("bags.id"), nullable=False, unique=True)
    weight_kg = db.Column(db.Numeric(8, 3), nullable=False)
    salt = db.Column(db.String(64), nullable=False)
    commitment = db.Column(db.String(64), nullable=False)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    fps_id = db.Column(db.Integer, db.ForeignKey("fps_locations.id"), nullable=False, index=True)
    request_id = db.Column(db.String(64), nullable=False, unique=True)
    tx_id = db.Column(db.String(80), nullable=True)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bag = db.relationship("Bag")
    operator = db.relationship("User", foreign_keys=[operator_id])
    fps = db.relationship("FpsLocation")


class ReconciliationRecord(db.Model):
    __tablename__ = "reconciliation_records"

    id = db.Column(db.Integer, primary_key=True)
    bag_pk = db.Column(db.Integer, db.ForeignKey("bags.id"), nullable=False, unique=True)
    dispatch_weight_kg = db.Column(db.Numeric(8, 3), nullable=False)
    receipt_weight_kg = db.Column(db.Numeric(8, 3), nullable=False)
    difference_kg = db.Column(db.Numeric(8, 3), nullable=False)
    tolerance_kg = db.Column(db.Numeric(6, 3), nullable=False)  # snapshot of the setting at that time
    status = db.Column(db.String(12), nullable=False, index=True)  # MATCHED | FLAGGED
    chain_status = db.Column(db.String(12), nullable=False, default="PENDING")  # PENDING | CONFIRMED
    chain_tx_id = db.Column(db.String(80), nullable=True)
    computed_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bag = db.relationship("Bag")


class Correction(db.Model):
    """Admin corrections are separate, append-only events - history is never overwritten."""
    __tablename__ = "corrections"

    id = db.Column(db.Integer, primary_key=True)
    bag_pk = db.Column(db.Integer, db.ForeignKey("bags.id"), nullable=False, index=True)
    note = db.Column(db.String(500), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    tx_id = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bag = db.relationship("Bag")
    admin = db.relationship("User")

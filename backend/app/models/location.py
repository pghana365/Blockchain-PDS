from ..extensions import db
from ..utils.shop_codes import new_fps_code, new_mls_code
from .constants import utcnow

# Administrative hierarchy (Telangana): District -> Mandal -> Village / ward.
#   MLS (Mandal Level Stock point): one stock point serving a mandal  -> mandal + district
#   FPS (Fair Price Shop):          a ration shop in a village or ward -> village + mandal + district
#
# Two identifiers per shop:
#   code       internal ledger id (MLS-01). Used in Fabric certificates (pds.location) and
#              chaincode authorisation. Never shown to MLS/FPS operators.
#   shop_code  random, permanent, externally visible code (MLS-9R21X). It is shown to an
#              operator only for that operator's own shop; neither operator sees the other
#              party's shop code.
# name, village, mandal and district are the real identity: visible to admin and auditor, and
# to an operator only for their OWN shop.


class MlsLocation(db.Model):
    __tablename__ = "mls_locations"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), nullable=False, unique=True)  # internal ledger id, e.g. MLS-01
    shop_code = db.Column(db.String(16), nullable=False, unique=True, default=new_mls_code)
    name = db.Column(db.String(120), nullable=False)
    mandal = db.Column(db.String(80), nullable=False)
    district = db.Column(db.String(80), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @property
    def area(self) -> str:
        return f"{self.mandal} mandal, {self.district} district"

    def __repr__(self):
        return f"<MLS {self.code}>"


class FpsLocation(db.Model):
    __tablename__ = "fps_locations"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), nullable=False, unique=True)  # internal ledger id, e.g. FPS-01
    shop_code = db.Column(db.String(16), nullable=False, unique=True, default=new_fps_code)
    name = db.Column(db.String(120), nullable=False)
    village = db.Column(db.String(120), nullable=True)            # village or town ward served
    mandal = db.Column(db.String(80), nullable=False)
    district = db.Column(db.String(80), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @property
    def area(self) -> str:
        place = f"{self.village}, " if self.village else ""
        return f"{place}{self.mandal} mandal, {self.district} district"

    def __repr__(self):
        return f"<FPS {self.code}>"


class LocationIdentityHistory(db.Model):
    """Append-only versions of a shop's real identity (name / village / mandal / district).

    Rows are only ever INSERTed. The single permitted change to an existing row is closing it:
    valid_to goes from NULL to the moment the next version starts (see models/guards.py).
    Rows are never deleted. shop_code and ledger_id are copied for reading; they never change.
    Visible to admin and auditor only."""
    __tablename__ = "location_identity_history"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(3), nullable=False)                 # 'MLS' | 'FPS'
    location_id = db.Column(db.Integer, nullable=False)            # mls_locations.id or fps_locations.id
    shop_code = db.Column(db.String(16), nullable=False)
    ledger_id = db.Column(db.String(16), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    village = db.Column(db.String(120), nullable=True)             # FPS only
    mandal = db.Column(db.String(80), nullable=False)
    district = db.Column(db.String(80), nullable=False)
    valid_from = db.Column(db.DateTime, nullable=False)
    valid_to = db.Column(db.DateTime, nullable=True)               # NULL = current version
    changed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)   # NULL = migration/seed
    reason = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    changer = db.relationship("User")

    __table_args__ = (
        db.Index("ix_loc_hist_location", "kind", "location_id", "valid_from"),
        db.UniqueConstraint("kind", "location_id", "valid_from", name="uq_loc_hist_version"),
    )

    @property
    def area(self) -> str:
        place = f"{self.village}, " if self.village else ""
        return f"{place}{self.mandal} mandal, {self.district} district"

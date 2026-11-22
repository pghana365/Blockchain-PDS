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
#   shop_code  random, permanent, externally visible code (MLS-9R21X). The only identifier
#              of the OTHER party an operator may ever see (MLS sees the destination FPS code;
#              FPS sees nothing about the origin).
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

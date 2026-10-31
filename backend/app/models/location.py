from ..extensions import db
from .constants import utcnow


class MlsLocation(db.Model):
    __tablename__ = "mls_locations"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), nullable=False, unique=True)  # e.g. MLS-01 (also the Fabric pds.location)
    name = db.Column(db.String(120), nullable=False)
    district = db.Column(db.String(80), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<MLS {self.code}>"


class FpsLocation(db.Model):
    __tablename__ = "fps_locations"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), nullable=False, unique=True)  # e.g. FPS-01
    name = db.Column(db.String(120), nullable=False)
    district = db.Column(db.String(80), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<FPS {self.code}>"

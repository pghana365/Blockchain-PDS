from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from .constants import ROLE_ADMIN, ROLE_AUDITOR, ROLE_FPS, ROLE_MLS, utcnow


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), nullable=False, unique=True)
    description = db.Column(db.String(200), nullable=False, default="")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), nullable=False, unique=True)  # employee id / username
    full_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False, index=True)
    mls_location_id = db.Column(db.Integer, db.ForeignKey("mls_locations.id"), nullable=True, index=True)
    fps_location_id = db.Column(db.Integer, db.ForeignKey("fps_locations.id"), nullable=True, index=True)
    fabric_identity = db.Column(db.String(64), nullable=True)  # enrollment id in the Fabric wallet
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    role = db.relationship("Role")
    mls_location = db.relationship("MlsLocation")
    fps_location = db.relationship("FpsLocation")

    # --- passwords: salted scrypt via werkzeug, never stored in plain text
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # --- Flask-Login
    @property
    def is_active(self):  # noqa: D401  (Flask-Login protocol)
        return bool(self.is_enabled)

    @property
    def role_name(self) -> str:
        return self.role.name

    @property
    def is_admin(self):
        return self.role_name == ROLE_ADMIN

    @property
    def is_mls(self):
        return self.role_name == ROLE_MLS

    @property
    def is_fps(self):
        return self.role_name == ROLE_FPS

    @property
    def is_auditor(self):
        return self.role_name == ROLE_AUDITOR

    @property
    def location_label(self) -> str:
        if self.is_mls and self.mls_location:
            return f"{self.mls_location.code} - {self.mls_location.name}"
        if self.is_fps and self.fps_location:
            return f"{self.fps_location.code} - {self.fps_location.name}"
        return "All locations (system-wide)"

    def __repr__(self):
        return f"<User {self.username} {self.role_name}>"

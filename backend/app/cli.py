"""flask seed        -> roles, setting, sample locations, demo users
   flask seed-bags   -> registers a few demo bags through the real workflow (needs Fabric)
"""
import click
from flask import current_app

from .extensions import db
from .models import FpsLocation, MlsLocation, Role, Setting, User
from .models.constants import (ALL_ROLES, ROLE_ADMIN, ROLE_AUDITOR, ROLE_DESCRIPTIONS, ROLE_FPS, ROLE_MLS)
from .services import audit, settings, workflow
from .utils.validators import validate_password

MLS = [("MLS-01", "Mandala Level Stock - Tirupati Rural", "Tirupati"),
       ("MLS-02", "Mandala Level Stock - Chittoor", "Chittoor")]
FPS = [("FPS-01", "Fair Price Shop 0123 - Renigunta", "Tirupati"),
       ("FPS-02", "Fair Price Shop 0456 - Srikalahasti", "Tirupati")]
# username, full name, role, MLS code, FPS code, fabric identity
USERS = [
    ("admin", "System Administrator", ROLE_ADMIN, None, None, "pdsadmin"),
    ("mls01.operator", "Ravi Kumar (MLS-01)", ROLE_MLS, "MLS-01", None, "mls01op"),
    ("mls02.operator", "Lakshmi Devi (MLS-02)", ROLE_MLS, "MLS-02", None, "mls02op"),
    ("fps01.operator", "Suresh Reddy (FPS-01)", ROLE_FPS, None, "FPS-01", "fps01op"),
    ("fps02.operator", "Anitha Rao (FPS-02)", ROLE_FPS, None, "FPS-02", "fps02op"),
    ("auditor1", "Meena Iyer (Auditor)", ROLE_AUDITOR, None, None, "auditor1"),
]


def register_cli(app):
    @app.cli.command("seed")
    def seed():
        """Create roles, tolerance setting, sample locations and demo users (idempotent)."""
        pw = current_app.config.get("SEED_PASSWORD")
        if not pw:
            raise click.ClickException("Set SEED_PASSWORD in .env (demo password for the sample users).")
        validate_password(pw)
        for name in ALL_ROLES:
            if not Role.query.filter_by(name=name).first():
                db.session.add(Role(name=name, description=ROLE_DESCRIPTIONS[name]))
        if not db.session.get(Setting, settings.TOLERANCE_KEY):
            db.session.add(Setting(key=settings.TOLERANCE_KEY, value=current_app.config["DEFAULT_TOLERANCE_KG"]))
        for code, name, dist in MLS:
            if not MlsLocation.query.filter_by(code=code).first():
                db.session.add(MlsLocation(code=code, name=name, district=dist))
        for code, name, dist in FPS:
            if not FpsLocation.query.filter_by(code=code).first():
                db.session.add(FpsLocation(code=code, name=name, district=dist))
        db.session.flush()
        made = []
        for username, full, role, mls, fps, ident in USERS:
            if User.query.filter_by(username=username).first():
                continue
            u = User(username=username, full_name=full, role_id=Role.query.filter_by(name=role).one().id,
                     mls_location_id=MlsLocation.query.filter_by(code=mls).one().id if mls else None,
                     fps_location_id=FpsLocation.query.filter_by(code=fps).one().id if fps else None,
                     fabric_identity=ident)
            u.set_password(pw)
            db.session.add(u)
            made.append(username)
        audit.log("SEED", None, "system", "seed", {"users_created": made}, username="system")
        db.session.commit()
        click.echo(f"Seeded. Users created: {', '.join(made) or 'none (already existed)'}")
        click.echo("Sample users share the SEED_PASSWORD from .env (demo only - change for real use).")

    @app.cli.command("seed-bags")
    @click.option("--count", default=5, show_default=True)
    def seed_bags(count):
        """Register demo bags BAG-1001.. (admin identity; real Fabric transactions)."""
        admin = User.query.filter_by(username="admin").one()
        mls, fps = MlsLocation.query.filter_by(code="MLS-01").one(), FpsLocation.query.filter_by(code="FPS-01").one()
        for i in range(count):
            bid = f"BAG-{1001 + i}"
            from .models import Bag
            if Bag.query.filter_by(bag_id=bid).first():
                click.echo(f"{bid} exists - skipped")
                continue
            bag = workflow.register_bag(admin, mls.id, fps.id, bid, "Rice", "CONS-DEMO-001")
            click.echo(f"registered {bag.bag_id} tx {(bag.register_tx_id or 'PENDING')[:16]}...")

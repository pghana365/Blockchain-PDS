"""Add the Telangana hierarchy: mandal for MLS and FPS, village/area for FPS.

An MLS (Mandal Level Stock point) serves a mandal; an FPS is a ration shop in a
village or ward of that mandal. The four demo shops are moved to real places:

  MLS-01  MLS Point - Miryalaguda                Miryalaguda mandal, Nalgonda
  FPS-01  Fair Price Shop 0123 - Avanthipuram    Avanthipuram, Miryalaguda mandal, Nalgonda
  MLS-02  MLS Point - Kodad                      Kodad mandal, Suryapet
  FPS-02  Fair Price Shop 0456 - Komarabanda     Komarabanda, Kodad mandal, Suryapet

A demo row is renamed only if it still has its previous seeded name and district.
Any other location gets its district copied into mandal so the column can be NOT NULL;
an admin should then correct it. IDs, shop codes, users, bags and history are untouched.

Revision ID: b7f3c2e91d05
Revises: 9c41e7d2f8a3
"""
from alembic import op
import sqlalchemy as sa

revision = "b7f3c2e91d05"
down_revision = "9c41e7d2f8a3"
branch_labels = None
depends_on = None

# table, ledger id, (old name, old district), (new name, village, mandal, district)
_DEMO = (
    ("mls_locations", "MLS-01", ("Mandala Level Stock - Nalgonda", "Nalgonda"),
     ("MLS Point - Miryalaguda", None, "Miryalaguda", "Nalgonda")),
    ("mls_locations", "MLS-02", ("Mandala Level Stock - Suryapet", "Suryapet"),
     ("MLS Point - Kodad", None, "Kodad", "Suryapet")),
    ("fps_locations", "FPS-01", ("Fair Price Shop 0123 - Miryalaguda", "Nalgonda"),
     ("Fair Price Shop 0123 - Avanthipuram", "Avanthipuram", "Miryalaguda", "Nalgonda")),
    ("fps_locations", "FPS-02", ("Fair Price Shop 0456 - Kodad", "Suryapet"),
     ("Fair Price Shop 0456 - Komarabanda", "Komarabanda", "Kodad", "Suryapet")),
)


def _table(name, with_village):
    cols = [sa.column("code"), sa.column("name"), sa.column("mandal"), sa.column("district")]
    return sa.table(name, *(cols + ([sa.column("village")] if with_village else [])))


def upgrade():
    with op.batch_alter_table("mls_locations") as b:
        b.add_column(sa.Column("mandal", sa.String(80), nullable=True))
    with op.batch_alter_table("fps_locations") as b:
        b.add_column(sa.Column("village", sa.String(120), nullable=True))
        b.add_column(sa.Column("mandal", sa.String(80), nullable=True))

    for table, code, (old_name, old_district), (name, village, mandal, district) in _DEMO:
        t = _table(table, table == "fps_locations")
        values = {"name": name, "mandal": mandal, "district": district}
        if table == "fps_locations":
            values["village"] = village
        op.execute(t.update().where(t.c.code == code, t.c.name == old_name, t.c.district == old_district)
                   .values(values))

    for table in ("mls_locations", "fps_locations"):
        t = _table(table, False)
        op.execute(t.update().where(t.c.mandal.is_(None)).values(mandal=t.c.district))
        with op.batch_alter_table(table) as b:
            b.alter_column("mandal", existing_type=sa.String(80), nullable=False)


def downgrade():
    for table, code, (old_name, old_district), (name, _, _, district) in _DEMO:
        t = _table(table, False)
        op.execute(t.update().where(t.c.code == code, t.c.name == name, t.c.district == district)
                   .values(name=old_name, district=old_district))
    with op.batch_alter_table("fps_locations") as b:
        b.drop_column("mandal")
        b.drop_column("village")
    with op.batch_alter_table("mls_locations") as b:
        b.drop_column("mandal")

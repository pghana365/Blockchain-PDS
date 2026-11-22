"""Option C: append-only location identity history; salted note commitments for corrections.

* location_identity_history: one version is recorded for every existing shop, valid from the
  shop's creation time, with reason "Initial version recorded at history introduction".
  The earlier demo-data corrections (Andhra Pradesh -> Telangana names, mandal/village added)
  were made by migrations before history existed; they are documented in those migration files
  and are not invented here as dated versions.
* corrections.note_salt: nullable; corrections recorded from now on put only a salted SHA-256
  commitment of the note on the ledger.

No existing row is modified or deleted: shop codes, ledger ids, consignment references,
bags, weights, transactions and audit entries are untouched.

Revision ID: c4a8e1f6b2d9
Revises: b7f3c2e91d05
"""
from alembic import op
import sqlalchemy as sa

revision = "c4a8e1f6b2d9"
down_revision = "b7f3c2e91d05"
branch_labels = None
depends_on = None

REASON = "Initial version recorded at history introduction"


def upgrade():
    op.create_table(
        "location_identity_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(3), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("shop_code", sa.String(16), nullable=False),
        sa.Column("ledger_id", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("village", sa.String(120), nullable=True),
        sa.Column("mandal", sa.String(80), nullable=False),
        sa.Column("district", sa.String(80), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "location_id", "valid_from", name="uq_loc_hist_version"),
    )
    op.create_index("ix_loc_hist_location", "location_identity_history", ["kind", "location_id", "valid_from"])

    conn = op.get_bind()
    hist = sa.table("location_identity_history", *[sa.column(c) for c in (
        "kind", "location_id", "shop_code", "ledger_id", "name", "village", "mandal", "district",
        "valid_from", "valid_to", "changed_by", "reason", "created_at")])
    rows = []
    for kind, table, has_village in (("MLS", "mls_locations", False), ("FPS", "fps_locations", True)):
        cols = "id, shop_code, code, name, mandal, district, created_at" + (", village" if has_village else "")
        for r in conn.execute(sa.text(f"SELECT {cols} FROM {table} ORDER BY id")).mappings():
            rows.append({"kind": kind, "location_id": r["id"], "shop_code": r["shop_code"], "ledger_id": r["code"],
                         "name": r["name"], "village": r.get("village") if has_village else None,
                         "mandal": r["mandal"], "district": r["district"], "valid_from": r["created_at"],
                         "valid_to": None, "changed_by": None, "reason": REASON, "created_at": sa.func.now()})
    for row in rows:
        conn.execute(hist.insert().values(**row))

    with op.batch_alter_table("corrections") as b:
        b.add_column(sa.Column("note_salt", sa.String(64), nullable=True))


def downgrade():
    with op.batch_alter_table("corrections") as b:
        b.drop_column("note_salt")
    op.drop_index("ix_loc_hist_location", table_name="location_identity_history")
    op.drop_table("location_identity_history")

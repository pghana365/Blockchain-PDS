"""Random shop codes for MLS/FPS locations and neutral consignment references.

Adds columns, backfills every existing row with a random value, then makes them
NOT NULL + UNIQUE. Primary keys, internal ledger ids (code), user assignments,
bags and all history are left unchanged.

Revision ID: 5d2e9a1c7b40
Revises: 84f0c6b2a719
"""
import secrets

from alembic import op
import sqlalchemy as sa

revision = "5d2e9a1c7b40"
down_revision = "84f0c6b2a719"
branch_labels = None
depends_on = None

# Frozen copy of app.utils.shop_codes so this migration never changes if that module does.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_TARGETS = (("mls_locations", "shop_code", "MLS", 5),
            ("fps_locations", "shop_code", "FPS", 5),
            ("consignments", "reference", "CNS", 8))


def _backfill(conn, table, column, prefix, length):
    t = sa.table(table, sa.column("id"), sa.column(column))
    used = {v for (v,) in conn.execute(sa.select(t.c[column]).where(t.c[column].isnot(None)))}
    for (row_id,) in conn.execute(sa.select(t.c.id).where(t.c[column].is_(None))).fetchall():
        while True:
            value = f"{prefix}-" + "".join(secrets.choice(_ALPHABET) for _ in range(length))
            if value not in used:
                break
        used.add(value)
        conn.execute(t.update().where(t.c.id == row_id).values({column: value}))


def upgrade():
    conn = op.get_bind()
    for table, column, prefix, length in _TARGETS:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.String(16), nullable=True))
        _backfill(conn, table, column, prefix, length)
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.String(16), nullable=False)
            batch.create_unique_constraint(f"uq_{table}_{column}", [column])


def downgrade():
    for table, column, _, _ in reversed(_TARGETS):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_{column}", type_="unique")
            batch.drop_column(column)

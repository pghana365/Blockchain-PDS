"""Remove internal ledger ids from the seeded demo operators' display names.

"Ravi Kumar (MLS-01)" -> "Ravi Kumar". Only rows whose name still equals the
original seed value are touched, so names an admin has changed are left alone.

Revision ID: 9c41e7d2f8a3
Revises: 5d2e9a1c7b40
"""
from alembic import op
import sqlalchemy as sa

revision = "9c41e7d2f8a3"
down_revision = "5d2e9a1c7b40"
branch_labels = None
depends_on = None

_NAMES = (("mls01.operator", "Ravi Kumar (MLS-01)", "Ravi Kumar"),
          ("mls02.operator", "Lakshmi Devi (MLS-02)", "Lakshmi Devi"),
          ("fps01.operator", "Suresh Reddy (FPS-01)", "Suresh Reddy"),
          ("fps02.operator", "Anitha Rao (FPS-02)", "Anitha Rao"))


def _rename(reverse=False):
    users = sa.table("users", sa.column("username"), sa.column("full_name"))
    for username, old, new in _NAMES:
        before, after = (new, old) if reverse else (old, new)
        op.execute(users.update().where(users.c.username == username, users.c.full_name == before)
                   .values(full_name=after))


def upgrade():
    _rename()


def downgrade():
    _rename(reverse=True)

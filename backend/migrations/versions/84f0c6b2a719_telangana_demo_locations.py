"""Rename the original demo locations to Telangana locations."""

from alembic import op
import sqlalchemy as sa

revision = "84f0c6b2a719"
down_revision = "3240294c2ba3"
branch_labels = None
depends_on = None

_LOCATIONS = (
    ("mls_locations", "MLS-01", ("Mandala Level Stock - Tirupati Rural", "Tirupati"),
     ("Mandala Level Stock - Nalgonda", "Nalgonda")),
    ("mls_locations", "MLS-02", ("Mandala Level Stock - Chittoor", "Chittoor"),
     ("Mandala Level Stock - Suryapet", "Suryapet")),
    ("fps_locations", "FPS-01", ("Fair Price Shop 0123 - Renigunta", "Tirupati"),
     ("Fair Price Shop 0123 - Miryalaguda", "Nalgonda")),
    ("fps_locations", "FPS-02", ("Fair Price Shop 0456 - Srikalahasti", "Tirupati"),
     ("Fair Price Shop 0456 - Kodad", "Suryapet")),
)


def _rename(reverse=False):
    for table_name, code, original, replacement in _LOCATIONS:
        before, after = (replacement, original) if reverse else (original, replacement)
        table = sa.table(table_name, sa.column("code"), sa.column("name"), sa.column("district"))
        op.execute(table.update().where(
            table.c.code == code, table.c.name == before[0], table.c.district == before[1]
        ).values(name=after[0], district=after[1]))


def upgrade():
    _rename()


def downgrade():
    _rename(reverse=True)

"""Generate database/schema.sql (MySQL DDL) from the SQLAlchemy models.

    cd backend && .venv\\Scripts\\python ..\\database\\export_schema.py

The authoritative schema history is the Alembic migration in backend/migrations;
schema.sql is a human-readable snapshot for reviewers and for manual setup.
"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost/pds")

from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app import models  # noqa: E402,F401
from app.extensions import db  # noqa: E402

lines = [
    "-- PDS Blind Weight Verification - MySQL 8 schema snapshot",
    "-- Generated from SQLAlchemy models by database/export_schema.py. Do not edit by hand.",
    "-- Apply with:  mysql -u pds -p pds < database/schema.sql   (or use `flask db upgrade`)",
    "",
    "SET NAMES utf8mb4;",
    "",
]
dialect = mysql.dialect()
for table in db.metadata.sorted_tables:
    ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
    ddl = ddl.replace("\n)\n", "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci\n") if "ENGINE" not in ddl else ddl
    lines.append(ddl + ";")
    for idx in sorted(table.indexes, key=lambda i: i.name):
        lines.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")
    lines.append("")

out = root / "database" / "schema.sql"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(db.metadata.sorted_tables)} tables)")

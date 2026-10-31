"""Drop all PDS tables, re-create them from the Alembic migration, and seed roles/locations/users.

    backend\\.venv\\Scripts\\python scripts\\reset_db.py          (asks for confirmation)
    backend\\.venv\\Scripts\\python scripts\\reset_db.py --yes
"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

root = Path(__file__).resolve().parent.parent
backend = root / "backend"
flask = backend / ".venv" / ("Scripts/flask.exe" if os.name == "nt" else "bin/flask")
load_dotenv(root / ".env")

if "--yes" not in sys.argv and input("This DELETES all PDS data in MySQL. Type 'reset' to continue: ") != "reset":
    sys.exit("aborted")

engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as conn:
    conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    tables = [r[0] for r in conn.execute(text("SHOW TABLES"))]
    for t in tables:
        conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
    conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
print(f"Dropped {len(tables)} tables.")

for args in (["db", "upgrade"], ["seed"]):
    subprocess.run([str(flask), "--app", "wsgi", *args], cwd=backend, check=True)
print("Database reset and seeded.")

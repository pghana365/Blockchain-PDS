"""Create .env from .env.example with freshly generated random secrets.

    python scripts/make_env.py          # creates .env if missing
    python scripts/make_env.py --force  # regenerate (changes DB password -> recreate DB volume!)
"""
import re
import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
src, dst = root / ".env.example", root / ".env"

if dst.exists() and "--force" not in sys.argv:
    print(".env already exists - leaving it untouched (use --force to regenerate).")
    sys.exit(0)

db_pw = secrets.token_urlsafe(18).replace("-", "A").replace("_", "b")
values = {
    "SECRET_KEY": secrets.token_hex(32),
    "MYSQL_ROOT_PASSWORD": secrets.token_urlsafe(18).replace("-", "A").replace("_", "b"),
    "MYSQL_PASSWORD": db_pw,
    "GATEWAY_API_KEY": secrets.token_hex(24),
    "SEED_PASSWORD": "Pds-" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y") + "7A",
}
text = src.read_text(encoding="utf-8")
for key, val in values.items():
    text = re.sub(rf"^{key}=.*$", f"{key}={val}", text, flags=re.M)
text = re.sub(r"^DATABASE_URL=.*$",
              f"DATABASE_URL=mysql+pymysql://pds:{db_pw}@127.0.0.1:3307/pds", text, flags=re.M)
dst.write_text(text, encoding="utf-8")
print(f"Created {dst} with random secrets.")

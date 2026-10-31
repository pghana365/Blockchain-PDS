from decimal import Decimal, InvalidOperation

from flask import current_app

from ..extensions import db
from ..models import Setting

TOLERANCE_KEY = "tolerance_kg"
MAX_TOLERANCE = Decimal("10")


def get_tolerance() -> Decimal:
    row = db.session.get(Setting, TOLERANCE_KEY)
    raw = row.value if row else current_app.config["DEFAULT_TOLERANCE_KG"]
    return Decimal(str(raw)).quantize(Decimal("0.001"))


def parse_tolerance(value) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Tolerance must be a number")
    if not d.is_finite() or d < 0 or d > MAX_TOLERANCE:
        raise ValueError(f"Tolerance must be between 0 and {MAX_TOLERANCE} kg")
    if d != d.quantize(Decimal("0.001")):
        raise ValueError("Tolerance may have at most 3 decimal places")
    return d.quantize(Decimal("0.001"))


def set_tolerance(value, user) -> Decimal:
    d = parse_tolerance(value)
    row = db.session.get(Setting, TOLERANCE_KEY)
    if row is None:
        row = Setting(key=TOLERANCE_KEY, value=str(d))
        db.session.add(row)
    row.value = str(d)
    row.updated_by = user.id
    return d

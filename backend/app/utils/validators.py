import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
LOCATION_CODE_RE = {"MLS": re.compile(r"^MLS-[A-Z0-9]{2,10}$"), "FPS": re.compile(r"^FPS-[A-Z0-9]{2,10}$")}
FABRIC_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


class ValidationError(ValueError):
    pass


def clean_text(value, label, min_len=1, max_len=120):
    v = (value or "").strip() if isinstance(value, str) else ""
    if not (min_len <= len(v) <= max_len):
        raise ValidationError(f"{label} must be {min_len}-{max_len} characters")
    if any(ord(c) < 32 for c in v):
        raise ValidationError(f"{label} contains invalid characters")
    return v


def validate_username(value):
    v = (value or "").strip() if isinstance(value, str) else ""
    if not USERNAME_RE.match(v):
        raise ValidationError("Username/employee ID: 3-40 characters (letters, digits, . _ -)")
    return v


def validate_password(value):
    if not isinstance(value, str) or len(value) < 10 or len(value) > 128:
        raise ValidationError("Password must be 10-128 characters")
    if not (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) and re.search(r"\d", value)):
        raise ValidationError("Password needs upper-case, lower-case and a digit")
    return value


def validate_location_code(kind, value):
    v = (value or "").strip().upper() if isinstance(value, str) else ""
    if not LOCATION_CODE_RE[kind].match(v):
        raise ValidationError(f"{kind} code must look like {kind}-01 (A-Z, 0-9)")
    return v


def validate_fabric_identity(value):
    v = (value or "").strip() if isinstance(value, str) else ""
    if v and not FABRIC_ID_RE.match(v):
        raise ValidationError("Fabric identity: 3-64 characters (letters, digits, . _ -)")
    return v or None

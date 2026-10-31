from datetime import datetime, timezone

ROLE_ADMIN = "ADMIN"
ROLE_MLS = "MLS_OPERATOR"
ROLE_FPS = "FPS_OPERATOR"
ROLE_AUDITOR = "AUDITOR"
ALL_ROLES = (ROLE_ADMIN, ROLE_MLS, ROLE_FPS, ROLE_AUDITOR)

ROLE_DESCRIPTIONS = {
    ROLE_ADMIN: "Administrator - manages users, locations, bags and settings",
    ROLE_MLS: "Mandala Level Stock operator - weighs and dispatches bags",
    ROLE_FPS: "Fair Price Shop operator - independently weighs and receives bags",
    ROLE_AUDITOR: "Auditor - read-only access to records and history",
}

# Role names used inside Fabric certificate attributes (pds.role)
CHAIN_ROLE = {
    ROLE_ADMIN: "admin",
    ROLE_MLS: "mls",
    ROLE_FPS: "fps",
    ROLE_AUDITOR: "auditor",
}

STATUS_REGISTERED = "REGISTERED"
STATUS_IN_TRANSIT = "IN_TRANSIT"
STATUS_RECEIVED = "RECEIVED"
STATUS_MATCHED = "MATCHED"
STATUS_FLAGGED = "FLAGGED"
BAG_STATUSES = (STATUS_REGISTERED, STATUS_IN_TRANSIT, STATUS_RECEIVED, STATUS_MATCHED, STATUS_FLAGGED)

MAX_BAG_WEIGHT_KG = 200  # sanity limit for a single gross-weight entry


def utcnow() -> datetime:
    """Naive UTC timestamp (portable across MySQL/SQLite)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

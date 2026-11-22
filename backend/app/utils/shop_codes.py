"""Random, externally visible identifiers.

A shop code (e.g. FPS-7K29Q) is what operators see instead of a shop's real name,
city or internal ledger id. It is generated with a CSPRNG, has no relation to the
location, and never changes. It is an identifier, not a secret or a permission:
authorisation is always by the user's assigned location id.
"""
import secrets

# no 0/O, 1/I/L - easy to read aloud and to copy from a label
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _random(prefix: str, length: int) -> str:
    return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_mls_code() -> str:
    return _random("MLS", 5)


def new_fps_code() -> str:
    return _random("FPS", 5)


def new_consignment_reference() -> str:
    """Neutral reference shown to operators. Contains no date, route or shop code."""
    return _random("CNS", 8)

"""Random identifiers. All are drawn with `secrets` (the OS CSPRNG), never `random`.

* Shop code (MLS-9R21X / FPS-7K29Q): permanent internal code of a shop. Admin/auditor only,
  plus the operator's own shop. It is an identifier, not a secret or a permission:
  authorisation is always by the user's assigned location id.
* Consignment reference (CNS-XXXXXXXXXXXX): what MLS and FPS operators see for a shipment.
  12 characters from a 31-symbol alphabet ~= 59 bits, so it cannot be guessed or enumerated.
* Ledger consignment id (CONS-XXXXXXXXXXXX): what goes on Fabric. Drawn independently of the
  reference, so the ledger cannot be joined to what operators see without the MySQL mapping.
"""
import secrets

# no 0/O, 1/I/L - easy to read aloud and to copy from a label
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERENCE_LENGTH = 12


def _random(prefix: str, length: int) -> str:
    return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_mls_code() -> str:
    return _random("MLS", 5)


def new_fps_code() -> str:
    return _random("FPS", 5)


def new_consignment_reference() -> str:
    """Neutral reference shown to operators. Contains no date, route or shop code."""
    return _random("CNS", REFERENCE_LENGTH)


def new_ledger_consignment_id() -> str:
    """Server-generated consignment id written to the ledger. Independent of the reference."""
    return _random("CONS", REFERENCE_LENGTH)

"""Bridge to the Node.js Fabric Gateway service."""
import hashlib
import secrets
from decimal import Decimal

import requests
from flask import current_app

from ..extensions import db
from ..models import BlockchainTransaction


class LedgerError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def new_salt() -> str:
    return secrets.token_hex(16)  # 128-bit CSPRNG salt


def new_request_id() -> str:
    return secrets.token_hex(16)


def fmt_weight(w: Decimal) -> str:
    return f"{Decimal(w):.3f}"


def commitment(bag_id: str, weight: Decimal, salt: str) -> str:
    """SHA-256(bagId|weight(3dp)|salt). Only this hash goes on the ledger."""
    return hashlib.sha256(f"{bag_id}|{fmt_weight(weight)}|{salt}".encode()).hexdigest()


class GatewayClient:
    def __init__(self, url, api_key, timeout=90):
        self.url, self.api_key, self.timeout = url.rstrip("/"), api_key, timeout

    def _post(self, path, identity, function, args):
        try:
            r = requests.post(
                f"{self.url}{path}",
                json={"identity": identity, "function": function, "args": [str(a) for a in args]},
                headers={"x-api-key": self.api_key}, timeout=self.timeout,
            )
        except requests.RequestException:
            raise LedgerError("Blockchain gateway is unavailable. Please try again shortly.", 503)
        try:
            body = r.json()
        except ValueError:
            raise LedgerError("Blockchain gateway returned an invalid response.", 502)
        if r.status_code >= 400:
            raise LedgerError(body.get("error", "Blockchain request failed"), 403 if r.status_code == 403 else 502)
        return body

    def submit(self, identity, function, args):
        return self._post("/submit", identity, function, args)

    def evaluate(self, identity, function, args):
        return self._post("/evaluate", identity, function, args)

    def identity(self, name):
        """Does this enrollment id exist in the gateway wallet? Returns {exists, role, location}."""
        try:
            r = requests.get(f"{self.url}/identity/{name}", headers={"x-api-key": self.api_key}, timeout=15)
            body = r.json()
        except (requests.RequestException, ValueError):
            raise LedgerError("Blockchain gateway is unavailable. Please try again shortly.", 503)
        if r.status_code >= 400 and r.status_code != 404:
            raise LedgerError(body.get("error", "Gateway error"), 502)
        return body


def get_ledger():
    ext = current_app.extensions
    if "ledger" not in ext:
        c = current_app.config
        ext["ledger"] = GatewayClient(c["GATEWAY_URL"], c["GATEWAY_API_KEY"], c["GATEWAY_TIMEOUT"])
    return ext["ledger"]


def chain_submit(user, bag, function, args, request_id):
    """Submit a transaction as the user's Fabric identity and record it in MySQL.

    On failure the current DB transaction is rolled back (so nothing half-done
    is persisted) and the failed attempt is stored in blockchain_transactions.
    """
    identity = user.fabric_identity
    bag_pk = bag.id if bag is not None else None
    user_id = user.id
    if not identity:
        raise LedgerError("Your account has no Fabric identity configured. Contact the administrator.", 409)
    try:
        out = get_ledger().submit(identity, function, list(args))
    except LedgerError as e:
        db.session.rollback()
        db.session.add(BlockchainTransaction(
            bag_pk=bag_pk, function=function, fabric_identity=identity, user_id=user_id,
            status="FAILED", error=e.message[:2000], request_id=request_id))
        db.session.commit()
        raise
    db.session.add(BlockchainTransaction(
        tx_id=out["txId"], bag_pk=bag_pk, function=function, fabric_identity=identity, user_id=user_id,
        block_number=int(out["blockNumber"]) if out.get("blockNumber") is not None else None,
        status="COMMITTED", request_id=request_id))
    return out


def chain_evaluate(user, function, args):
    if not user.fabric_identity:
        raise LedgerError("No Fabric identity configured for this account.", 409)
    return get_ledger().evaluate(user.fabric_identity, function, list(args))

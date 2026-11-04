"""Bridge to the Node.js Fabric Gateway service.

Every write to Fabric ends in exactly one of three states (never silently assumed):
  COMMITTED - the ledger confirmed the transaction (VALID in a block)
  FAILED    - the transaction is certainly not on the ledger (rejected / invalidated)
  UNKNOWN   - we did not see the commit status (timeout, network error). It MAY still
              commit, so callers must neither roll back nor blindly resend; they ask the
              ledger (tx_status / on-chain bag state) first.
"""
import hashlib
import secrets
import time
from decimal import Decimal

import requests
from flask import current_app

from ..extensions import db
from ..models import BlockchainTransaction

COMMITTED, FAILED, UNKNOWN = "COMMITTED", "FAILED", "UNKNOWN"


class LedgerError(Exception):
    def __init__(self, message, status=502, outcome=FAILED, tx_id=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.outcome = outcome      # FAILED (certainly not committed) | UNKNOWN (may still commit)
        self.tx_id = tx_id


class TxResult:
    """Result of one Fabric write attempt."""

    def __init__(self, state, tx_id=None, block=None, error=None, status=502):
        self.state, self.tx_id, self.block, self.error, self.status = state, tx_id, block, error, status

    def __repr__(self):
        return f"<TxResult {self.state} tx={self.tx_id}>"


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

    def _request(self, method, path, payload=None, timeout=None):
        try:
            r = requests.request(method, f"{self.url}{path}", json=payload,
                                 headers={"x-api-key": self.api_key}, timeout=(5, timeout or self.timeout))
        except requests.exceptions.ConnectTimeout:
            raise LedgerError("Blockchain gateway is unavailable. Please try again shortly.", 503, FAILED)
        except requests.exceptions.ConnectionError as e:
            text = repr(e)
            refused = "NewConnectionError" in text or "Connection refused" in text or "actively refused" in text
            if refused:  # never reached the gateway: certainly nothing was submitted
                raise LedgerError("Blockchain gateway is unavailable. Please try again shortly.", 503, FAILED)
            raise LedgerError("Lost contact with the blockchain gateway; the outcome is unknown.", 503, UNKNOWN)
        except requests.RequestException:
            # request was sent but no (complete) answer came back: the transaction may have been processed
            raise LedgerError("The blockchain gateway did not answer in time; the outcome is unknown.", 503, UNKNOWN)
        try:
            body = r.json()
        except ValueError:
            raise LedgerError("Blockchain gateway returned an invalid response.", 502, UNKNOWN if r.status_code == 504 else FAILED)
        if r.status_code >= 400:
            outcome = body.get("outcome") or (UNKNOWN if r.status_code == 504 else FAILED)
            raise LedgerError(body.get("error", "Blockchain request failed"), 403 if r.status_code == 403 else 502,
                              outcome, body.get("txId"))
        return body

    def submit(self, identity, function, args):
        return self._request("POST", "/submit", {"identity": identity, "function": function, "args": [str(a) for a in args]})

    def evaluate(self, identity, function, args):
        return self._request("POST", "/evaluate", {"identity": identity, "function": function, "args": [str(a) for a in args]})

    def tx_status(self, identity, tx_id):
        """Ask the peer about a transaction: {state: VALID|INVALID|NOT_FOUND, blockNumber?, code?}."""
        return self._request("POST", "/tx-status", {"identity": identity, "txId": tx_id}, timeout=30)

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


def tx_status(identity, tx_id):
    """VALID / INVALID / NOT_FOUND, or None if the ledger could not be asked right now."""
    try:
        return get_ledger().tx_status(identity, tx_id)
    except LedgerError:
        return None


def submit_tx(identity, function, args) -> TxResult:
    """Send one transaction and return its VERIFIED outcome. Does not touch the database.

    If the gateway reports that the commit outcome is unknown (timeout etc.), the ledger is
    asked for that transaction id a few times - this absorbs ordinary delayed block commits.
    Only a transaction the ledger reports as VALID is returned as COMMITTED."""
    try:
        out = get_ledger().submit(identity, function, list(args))
    except LedgerError as e:
        if e.outcome != UNKNOWN:
            return TxResult(FAILED, None, None, e.message, e.status)
        return _confirm_unknown(identity, e)
    block = int(out["blockNumber"]) if out.get("blockNumber") is not None else None
    return TxResult(COMMITTED, out["txId"], block)


def _confirm_unknown(identity, err) -> TxResult:
    if not err.tx_id:
        return TxResult(UNKNOWN, None, None, err.message)
    cfg = current_app.config
    polls, interval = int(cfg.get("LEDGER_CONFIRM_POLLS", 4)), float(cfg.get("LEDGER_CONFIRM_INTERVAL", 2.0))
    for attempt in range(polls):
        st = tx_status(identity, err.tx_id)
        if st and st.get("state") == "VALID":
            blk = st.get("blockNumber")
            return TxResult(COMMITTED, err.tx_id, int(blk) if blk is not None else None)
        if st and st.get("state") == "INVALID":
            return TxResult(FAILED, err.tx_id, None, f"Transaction was rejected at commit ({st.get('code')}).", 409)
        if attempt < polls - 1 and interval:
            time.sleep(interval)
    return TxResult(UNKNOWN, err.tx_id, None, err.message)


def chain_submit(user, bag, function, args, request_id):
    """Submit a transaction as the user's Fabric identity and record it in MySQL.

    Used for operations whose local effect can simply be abandoned (GrantRole, corrections).
    Bag lifecycle writes (register / dispatch / receive / reconcile) use the write-ahead
    flow in workflow.py instead, which can recover from an unknown outcome.

    COMMITTED -> a COMMITTED row is added to the session (caller commits).
    FAILED    -> DB rolled back, FAILED row stored, error raised.
    UNKNOWN   -> DB rolled back, a PENDING row (with the tx id if known) stored so the
                 attempt stays visible, error raised that says the outcome is unknown.
    """
    identity = user.fabric_identity
    bag_pk = bag.id if bag is not None else None
    user_id = user.id
    if not identity:
        raise LedgerError("Your account has no Fabric identity configured. Contact the administrator.", 409)
    res = submit_tx(identity, function, args)
    if res.state == COMMITTED:
        db.session.add(BlockchainTransaction(
            tx_id=res.tx_id, bag_pk=bag_pk, function=function, fabric_identity=identity, user_id=user_id,
            block_number=res.block, status="COMMITTED", request_id=request_id))
        return {"txId": res.tx_id, "blockNumber": res.block}
    db.session.rollback()
    if res.state == UNKNOWN:
        db.session.add(BlockchainTransaction(
            tx_id=res.tx_id, bag_pk=bag_pk, function=function, fabric_identity=identity, user_id=user_id,
            status="PENDING", error=(res.error or "")[:2000], request_id=request_id))
        db.session.commit()
        raise LedgerError("The blockchain did not confirm this operation in time and its outcome is unknown. "
                          "Nothing was saved here; check the transaction history before repeating it.",
                          502, UNKNOWN, res.tx_id)
    db.session.add(BlockchainTransaction(
        bag_pk=bag_pk, function=function, fabric_identity=identity, user_id=user_id,
        status="FAILED", error=(res.error or "")[:2000], request_id=request_id))
    db.session.commit()
    raise LedgerError(res.error or "Blockchain request failed", res.status, FAILED)


def chain_evaluate(user, function, args):
    if not user.fabric_identity:
        raise LedgerError("No Fabric identity configured for this account.", 409)
    return get_ledger().evaluate(user.fabric_identity, function, list(args))

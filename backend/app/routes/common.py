from functools import wraps

from flask import jsonify, request
from flask_login import current_user

from ..extensions import db
from ..services import audit
from ..services.ledger import LedgerError
from ..services.workflow import WorkflowError
from ..utils.validators import ValidationError


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def api_guard(fn):
    """Uniform JSON errors; rejected/forbidden attempts are written to the audit log."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WorkflowError as e:
            db.session.rollback()
            if e.status in (403, 404, 409):
                audit.log("REQUEST_REJECTED", current_user, "route", request.path,
                          {"code": e.code, "message": e.message}, commit=True)
            return jsonify(error=e.message, code=e.code), e.status
        except ValidationError as e:
            db.session.rollback()
            return jsonify(error=str(e), code="INVALID_INPUT"), 400
        except LedgerError as e:
            db.session.rollback()
            audit.log("LEDGER_ERROR", current_user, "route", request.path, {"message": e.message[:300]}, commit=True)
            return jsonify(error=e.message, code="LEDGER_ERROR"), (e.status if e.status in (403, 409, 503) else 502)
    return wrapper

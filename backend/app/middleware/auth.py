from functools import wraps

from flask import jsonify, render_template, request
from flask_login import current_user

from ..extensions import db, login_manager
from ..models import User
from ..services import audit


@login_manager.user_loader
def load_user(user_id):
    u = db.session.get(User, int(user_id))
    return u if u and u.is_enabled else None   # disabled accounts are logged out immediately


def wants_json():
    return request.path.startswith("/api/") or request.is_json


@login_manager.unauthorized_handler
def unauthorized():
    if wants_json():
        return jsonify(error="Authentication required", code="UNAUTHENTICATED"), 401
    from flask import redirect, url_for
    # keep the scanned QR URL so the user lands on the bag after logging in
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def forbidden(message="You are not authorised to perform this action"):
    db.session.rollback()
    audit.log("ACCESS_DENIED", current_user if current_user.is_authenticated else None,
              "route", request.path, {"method": request.method, "reason": message}, commit=True)
    if wants_json():
        return jsonify(error=message, code="FORBIDDEN"), 403
    return render_template("error.html", code=403, message=message), 403


def roles_required(*roles):
    """Allow only users with one of the given role names."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role_name not in roles:
                return forbidden()
            return fn(*args, **kwargs)
        return wrapper
    return deco

from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User
from ..models.constants import utcnow
from ..services import audit

bp = Blueprint("auth", __name__)

_DUMMY_HASH = None


def _dummy_check(password):
    """Spend comparable time when the user does not exist (blunts user enumeration by timing)."""
    global _DUMMY_HASH
    from werkzeug.security import check_password_hash, generate_password_hash
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("dummy-password-value", method="scrypt")
    check_password_hash(_DUMMY_HASH, password)


def safe_next(target):
    """Only allow same-site relative redirects (no open redirect)."""
    if not target:
        return None
    p = urlparse(target)
    if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def home_for(user):
    return url_for("pages.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if current_user.is_authenticated:
        return redirect(nxt or home_for(current_user))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:40]
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first() if username else None
        cfg = current_app.config
        now = utcnow()

        if user is None:
            _dummy_check(password)
            audit.log("LOGIN_FAILED", None, "user", username[:40], {"reason": "unknown user"}, commit=True, username=username[:40] or None)
            error = "Invalid username or password."
        elif user.locked_until and user.locked_until > now:
            audit.log("LOGIN_BLOCKED", user, "user", user.username, {"reason": "locked"}, commit=True)
            error = "Account temporarily locked after repeated failures. Try again later."
        elif not user.is_enabled or not user.check_password(password):
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= cfg["MAX_FAILED_LOGINS"]:
                user.locked_until = now + timedelta(minutes=cfg["LOCKOUT_MINUTES"])
                user.failed_logins = 0
            audit.log("LOGIN_FAILED", user, "user", user.username, {"reason": "bad credentials or disabled"}, commit=True)
            error = "Invalid username or password."
        else:
            user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
            session.clear()                     # prevent session fixation
            login_user(user, remember=False)
            session.permanent = True
            audit.log("LOGIN_SUCCESS", user, "user", user.username, commit=True)
            return redirect(nxt or home_for(user))

    return render_template("login.html", error=error, next=nxt or ""), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.log("LOGOUT", current_user, "user", current_user.username, commit=True)
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))

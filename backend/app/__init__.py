from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError

from config import Config
from .extensions import csrf, db, login_manager, migrate


def create_app(config_class=Config):
    config_class.validate()
    app = Flask(__name__, template_folder=config_class.TEMPLATE_DIR, static_folder=config_class.STATIC_DIR,
                static_url_path="/static")
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(__import__("pathlib").Path(__file__).resolve().parent.parent / "migrations"))
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.session_protection = "strong"
    login_manager.login_view = "auth.login"

    from . import models  # noqa: F401  (register tables)
    from .middleware import auth as _auth  # noqa: F401  (user_loader / unauthorized handler)
    from .routes import api_admin, api_audit, api_ops, auth, pages

    for module in (auth, pages, api_ops, api_admin, api_audit):
        app.register_blueprint(module.bp)

    register_hooks(app)
    from .cli import register_cli
    register_cli(app)
    return app


def register_hooks(app):
    from datetime import datetime

    @app.template_filter("dt")
    def fmt_dt(v):
        if not v:
            return "-"
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.rstrip("Z"))
            except ValueError:
                return v
        return v.strftime("%d %b %Y, %H:%M:%S") + " UTC"

    @app.before_request
    def reset_cached_user():
        # Never reuse a user cached on `g` by an earlier request that shared this app context.
        from flask import g
        g.pop("_login_user", None)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store" if not request.path.endswith(".png") else resp.headers.get("Cache-Control", "no-store")
        return resp

    from .services.workflow import WorkflowError

    @app.errorhandler(WorkflowError)
    def workflow_error(e):
        db.session.rollback()
        if request.path.startswith("/api/"):
            return jsonify(error=e.message, code=e.code), e.status
        return render_template("error.html", code=e.status, message=e.message), e.status

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Security token missing or expired. Reload the page.", code="CSRF"), 400
        return render_template("error.html", code=400, message="Your session expired. Reload the page and try again."), 400

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found", code="NOT_FOUND"), 404
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(405)
    def not_allowed(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Method not allowed", code="METHOD"), 405
        return render_template("error.html", code=405, message="Method not allowed."), 405

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error", code="SERVER"), 500
        return render_template("error.html", code=500, message="Something went wrong on our side."), 500

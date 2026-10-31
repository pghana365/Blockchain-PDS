from flask import Blueprint, current_app, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..middleware.auth import forbidden, roles_required
from ..models import (AuditLog, Bag, BlockchainTransaction, FpsLocation, MlsLocation, Role, User)
from ..models.constants import (BAG_STATUSES, ROLE_ADMIN, ROLE_AUDITOR, ROLE_FPS, ROLE_MLS)
from ..services import access, audit, reports, settings, workflow
from ..services.serializers import bag_view

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    return redirect(url_for("pages.dashboard") if current_user.is_authenticated else url_for("auth.login"))


@bp.get("/favicon.ico")
def favicon():
    return "", 204


@bp.get("/dashboard")
@login_required
def dashboard():
    u = current_user
    if u.is_admin:
        return render_template("admin/dashboard.html", stats=reports.admin_stats(),
                               recent=reports.audit_rows(limit=8), flagged=reports.discrepancy_rows(u)[:5],
                               tolerance=settings.get_tolerance())
    if u.is_mls:
        rows = reports.mls_rows(u, request.args.get("status") or None)
        return render_template("mls/dashboard.html", rows=rows, statuses=BAG_STATUSES,
                               sel_status=request.args.get("status", ""))
    if u.is_fps:
        return render_template("fps/dashboard.html", incoming=reports.fps_incoming(u), receipts=reports.fps_receipts(u))
    if u.is_auditor:
        q, status = request.args.get("q", "").strip(), request.args.get("status", "")
        return render_template("auditor/dashboard.html", stats=reports.admin_stats(),
                               rows=reports.audit_rows(q or None, status or None), q=q, sel_status=status,
                               statuses=BAG_STATUSES, integrity=audit.verify_chain())
    return forbidden()


@bp.get("/scan")
@login_required
def scan():
    return render_template("scan.html")


@bp.get("/b/<bag_id>")
@bp.get("/bags/<bag_id>")
@login_required
def bag_detail(bag_id):
    """Landing page for a scanned QR code. Same URL for MLS and FPS - the content is
    decided by the authenticated role and location, never by the URL."""
    try:
        bid = workflow.normalize_bag_id(bag_id)
    except workflow.WorkflowError:
        return render_template("error.html", code=404, message="This is not a valid PDS bag code."), 404
    bag = Bag.query.filter_by(bag_id=bid).one_or_none()
    if bag is None or not access.can_view_bag(current_user, bag):
        audit.log("BAG_VIEW_DENIED", current_user, "bag", bid, {"role": current_user.role_name}, commit=True)
        return render_template("error.html", code=404,
                               message="Bag not found, or it is not associated with your location."), 404
    audit.log("BAG_VIEWED", current_user, "bag", bid, commit=True)
    return render_template("bag_detail.html", bag=bag_view(current_user, bag), tolerance=settings.get_tolerance())


@bp.get("/reports/discrepancies")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
def discrepancies():
    show_all = request.args.get("all") == "1"
    rows = reports.discrepancy_rows(current_user, only_flagged=not show_all)
    return render_template("reports/discrepancies.html", rows=rows, show_all=show_all,
                           tolerance=settings.get_tolerance())


@bp.get("/transactions")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
def transactions():
    page = max(1, request.args.get("page", 1, type=int))
    q = reports.transactions_query(current_user, request.args.get("bag") or None,
                                   request.args.get("function") or None, request.args.get("status") or None)
    pg = q.paginate(page=page, per_page=25, error_out=False)
    return render_template("transactions.html", pg=pg, args=request.args)


@bp.get("/audit-logs")
@roles_required(ROLE_ADMIN, ROLE_AUDITOR)
def audit_logs():
    page = max(1, request.args.get("page", 1, type=int))
    q = AuditLog.query
    if request.args.get("action"):
        q = q.filter(AuditLog.action == request.args["action"][:40])
    if request.args.get("user"):
        q = q.filter(AuditLog.username == request.args["user"][:40])
    pg = q.order_by(AuditLog.id.desc()).paginate(page=page, per_page=30, error_out=False)
    actions = [a for (a,) in AuditLog.query.with_entities(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    return render_template("audit_logs.html", pg=pg, args=request.args, actions=actions, integrity=audit.verify_chain())


# ------------------------------------------------------------------ admin pages
@bp.get("/admin/users")
@roles_required(ROLE_ADMIN)
def admin_users():
    return render_template("admin/users.html", users=User.query.order_by(User.id).all(),
                           roles=Role.query.order_by(Role.id).all(),
                           mls=MlsLocation.query.filter_by(is_active=True).order_by(MlsLocation.code).all(),
                           fps=FpsLocation.query.filter_by(is_active=True).order_by(FpsLocation.code).all())


@bp.get("/admin/locations")
@roles_required(ROLE_ADMIN)
def admin_locations():
    return render_template("admin/locations.html", mls=MlsLocation.query.order_by(MlsLocation.code).all(),
                           fps=FpsLocation.query.order_by(FpsLocation.code).all())


@bp.get("/admin/bags")
@roles_required(ROLE_ADMIN)
def admin_bags():
    q, status = request.args.get("q", "").strip(), request.args.get("status", "")
    return render_template("admin/bags.html", rows=reports.audit_rows(q or None, status or None),
                           q=q, sel_status=status, statuses=BAG_STATUSES,
                           mls=MlsLocation.query.filter_by(is_active=True).order_by(MlsLocation.code).all(),
                           fps=FpsLocation.query.filter_by(is_active=True).order_by(FpsLocation.code).all(),
                           next_id=workflow.next_bag_id())


@bp.get("/admin/bags/<bag_id>/label")
@login_required
def bag_label(bag_id):
    """Printable QR label (admin, or an operator for bags at their own location)."""
    bid = workflow.normalize_bag_id(bag_id)
    bag = Bag.query.filter_by(bag_id=bid).one_or_none()
    if bag is None or not access.can_view_bag(current_user, bag):
        return render_template("error.html", code=404, message="Bag not found."), 404
    return render_template("label.html", bag=bag)


@bp.get("/admin/labels")
@roles_required(ROLE_ADMIN)
def bag_labels():
    """Print sheet of QR labels for many bags."""
    ids = [workflow.normalize_bag_id(x) for x in request.args.get("ids", "").split(",") if x.strip()][:60]
    bags = Bag.query.filter(Bag.bag_id.in_(ids)).order_by(Bag.bag_id).all() if ids else []
    return render_template("labels_sheet.html", bags=bags)


@bp.get("/admin/settings")
@roles_required(ROLE_ADMIN)
def admin_settings():
    return render_template("admin/settings.html", tolerance=settings.get_tolerance())

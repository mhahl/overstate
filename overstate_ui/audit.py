"""Audit trail. Every mutating action records who, what, and which JID."""

from flask import Blueprint, render_template, request
from flask_login import login_required

from .db import get_session
from .models import AuditEvent

bp = Blueprint("audit", __name__, url_prefix="/audit")


def log_event(user: str, action: str, jid: str | None = None) -> AuditEvent:
    session = get_session()
    event = AuditEvent(user=user, action=action, jid=jid)
    session.add(event)
    session.commit()
    return event


@bp.route("/")
@login_required
def index():
    """Newest-first event list with user/action filters and pagination."""
    from .settings import get_setting

    user = request.args.get("user", "").strip()
    action = request.args.get("action", "").strip()
    try:
        page_size = int(get_setting("page_size"))
    except ValueError:
        page_size = 25
    if page_size not in (10, 25, 50):
        page_size = 25
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    query = get_session().query(AuditEvent)
    if user:
        query = query.filter(AuditEvent.user.contains(user))
    if action:
        query = query.filter(AuditEvent.action.contains(action))
    total = query.count()
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    sort = request.args.get("sort", "when")
    if sort not in ("when", "user", "action"):
        sort = "when"
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    order = {
        "when": AuditEvent.id,
        "user": AuditEvent.user,
        "action": AuditEvent.action,
    }[sort]
    order = order.desc() if direction == "desc" else order.asc()
    events = query.order_by(order).offset((page - 1) * page_size).limit(page_size).all()
    return render_template(
        "audit.html",
        events=events,
        user=user,
        action=action,
        page=page,
        pages=pages,
        total=total,
        sort=sort,
        direction=direction,
    )

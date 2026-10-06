"""Audit trail. Every mutating action records who, what, and which JID."""

from flask import Blueprint, render_template, request
from flask_login import login_required

from .db import get_session
from .models import AuditEvent

bp = Blueprint("audit", __name__, url_prefix="/audit")


def redact_constrained_detail(detail: str | None) -> str | None:
    """Redact a ``job-constrained`` detail (a dropped-minion id list for
    fleet audit) to an anonymous count for non-fleet readers."""
    if not detail:
        return detail
    dropped = [p for p in detail.split(",") if p.strip()]
    return f"{len(dropped)} minion(s) outside the actor's scope"


def log_event(
    user: str,
    action: str,
    jid: str | None = None,
    *,
    outcome: str | None = None,
    permission: str | None = None,
    minion_id: str | None = None,
    detail: str | None = None,
) -> AuditEvent:
    session = get_session()
    event = AuditEvent(
        user=user,
        action=action,
        jid=jid,
        outcome=outcome,
        permission=permission,
        minion_id=minion_id,
        detail=detail,
    )
    session.add(event)
    session.commit()
    return event


@bp.route("/")
@login_required
def index():
    """Newest-first event list with user/action filters and pagination."""
    from flask_login import current_user

    from .authz import has_fleet, perm_scope_minions, rbac_mode, require
    from .settings import get_setting

    require("audit.read")
    scoped = rbac_mode() == "scoped"
    fleet_audit = not scoped or has_fleet(current_user, "audit.read")

    user = request.args.get("user", "").strip()
    action = request.args.get("action", "").strip()
    outcome = request.args.get("outcome", "").strip()
    if outcome not in ("allow", "deny"):
        outcome = ""
    permission = request.args.get("permission", "").strip()
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
    if scoped and not fleet_audit:
        from sqlalchemy import or_

        # Own rows plus rows naming an in-scope minion; null-minion rows
        # (fleet history) stay fleet-only. Scoped to the audit.read
        # grants, not every grant: a wider grant without audit.read
        # must not widen this view.
        allowed = perm_scope_minions(current_user, "audit.read")
        query = query.filter(
            or_(
                AuditEvent.user == current_user.username,
                AuditEvent.minion_id.in_(allowed) if allowed else False,
            )
        )
    if user:
        query = query.filter(AuditEvent.user.contains(user))
    if action:
        query = query.filter(AuditEvent.action.contains(action))
    if outcome == "deny":
        query = query.filter(AuditEvent.outcome == "deny")
    elif outcome == "allow":
        # Old rows have null outcome and count as allow.
        query = query.filter(
            (AuditEvent.outcome == "allow") | (AuditEvent.outcome.is_(None))
        )
    if permission:
        query = query.filter(AuditEvent.permission.contains(permission))
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
    if scoped and not fleet_audit:
        # job-constrained detail names dropped (out-of-scope) minions for
        # fleet audit: redact to a count even on the actor's own rows.
        # Detach first: the teardown commits, and the redaction must never
        # persist back to the fleet audit trail.
        session = get_session()
        for event in events:
            if event.action == "job-constrained" and event.detail:
                session.expunge(event)
                event.detail = redact_constrained_detail(event.detail)
    return render_template(
        "audit.html",
        events=events,
        user=user,
        action=action,
        outcome=outcome,
        permission=permission,
        page=page,
        pages=pages,
        total=total,
        sort=sort,
        direction=direction,
    )

"""Dashboard: live counts from salt-api, history from Postgres."""

import httpx
from flask import Blueprint, current_app, render_template
from flask_login import login_required

from .db import get_session
from .models import Job, JobReturn, Minion
from .salt_client import SaltApiError, SaltClient

bp = Blueprint("dashboard", __name__)


def get_salt() -> SaltClient:
    return current_app.extensions["salt_client"]


def collect_stats(client: SaltClient, overview: dict | None = None) -> dict:
    """Live key/presence counts; falls back to unreachable marker.

    Pass a worker-computed `overview` to skip the Salt calls; None
    runs them synchronously (fallback and test path).
    """
    from .tasks import salt_overview_now

    stats: dict = {"reachable": False}
    try:
        stats.update(overview if overview is not None
                     else salt_overview_now(client))
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        pass
    session = get_session()
    stats["in_flight"] = session.query(Job).filter_by(complete=False).count()
    stats["last_failures"] = (
        session.query(JobReturn)
        .filter_by(success=False)
        .order_by(JobReturn.id.desc())
        .limit(5)
        .all()
    )
    return stats


@bp.route("/")
@login_required
def index():
    from .tasks import (CAPABILITY_CHECKS, capabilities_task,
                        probe_capabilities, queue_or_none,
                        read_capability_cache, salt_overview_task, wait_for)

    client = get_salt()
    overview: dict | None = None
    job = queue_or_none(salt_overview_task)
    if job is not None:
        status, value = wait_for(job, wait=6.0)
        if status == "ready":
            overview = value
    stats = collect_stats(client, overview=overview)
    caps = read_capability_cache()
    if caps is None:
        target = ping_target()
        probe_job = queue_or_none(capabilities_task, target)
        if probe_job is not None:
            status, value = wait_for(probe_job, wait=5.0)
            if status == "ready":
                caps = value
        if caps is None:
            caps = probe_capabilities(client, target)
    health = {"url": client.base_url, "token_age": client.token_age,
              "wheel_ok": caps["wheel_ok"], "runner_ok": caps["runner_ok"],
              "reachable": stats["reachable"], "error": caps.get("error")}
    checks = [{**c, "ok": bool(caps.get(c["key"]))}
              for c in CAPABILITY_CHECKS]
    return render_template("dashboard.html", stats=stats, health=health,
                           checks=checks)


def ping_target() -> str | None:
    """One accepted minion for the execution-door probe, or None."""
    row = (get_session().query(Minion.id).order_by(Minion.id).first())
    return row[0] if row else None

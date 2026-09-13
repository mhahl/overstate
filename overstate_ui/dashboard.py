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


def snapshot_versions() -> dict[str, int]:
    """{saltversion: count} from the grain snapshot cache. Fallback
    when the master won't answer manage.versions."""
    counts: dict[str, int] = {}
    for row in get_session().query(Minion).all():
        version = (row.grains or {}).get("saltversion")
        if version:
            version = str(version)
            counts[version] = counts.get(version, 0) + 1
    return counts


def collect_stats(client: SaltClient, overview: dict | None = None,
                  truth: dict | None = None) -> dict:
    """Live key/presence counts; falls back to unreachable marker.

    Pass worker-computed `overview`/`truth` to skip the Salt calls;
    None runs them synchronously (fallback and test path).
    """
    from .tasks import fleet_truth_now, salt_overview_now

    stats: dict = {"reachable": False}
    try:
        stats.update(overview if overview is not None
                     else salt_overview_now(client))
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        pass
    if truth is None:
        truth = fleet_truth_now(client)
    session = get_session()
    incomplete = {r[0] for r in
                  session.query(Job.jid).filter_by(complete=False).all()}
    if truth.get("active_live"):
        active = set(truth.get("active_jids") or [])
        stats["in_flight"] = len(incomplete & active)
        stats["in_flight_live"] = True
    else:
        stats["in_flight"] = len(incomplete)
        stats["in_flight_live"] = False
    if truth.get("versions"):
        stats["versions"] = truth["versions"]
        stats["versions_live"] = True
    else:
        stats["versions"] = snapshot_versions()
        stats["versions_live"] = False
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
                        fleet_truth_task, probe_capabilities, queue_or_none,
                        read_capability_cache, salt_overview_task, wait_for)

    client = get_salt()
    overview: dict | None = None
    job = queue_or_none(salt_overview_task)
    if job is not None:
        status, value = wait_for(job, wait=6.0)
        if status == "ready":
            overview = value
    truth: dict | None = None
    truth_job = queue_or_none(fleet_truth_task)
    if truth_job is not None:
        status, value = wait_for(truth_job, wait=6.0)
        if status == "ready":
            truth = value
    stats = collect_stats(client, overview=overview, truth=truth)
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

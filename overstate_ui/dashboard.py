"""Dashboard: snapshot shell rendered instantly, live panels hydrated
by polling. No Salt I/O happens in these request paths — only fast DB
reads and Redis job lookups — so a slow or sick master can never pin a
gunicorn worker. History always comes from Postgres."""

import datetime as dt
import time
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func

from .audit import log_event
from .auth import roles_required
from .db import get_session
from .models import Job, JobReturn, Minion, SaltReturn
from .salt_client import SaltClient
from .tasks_queue import describe_job

bp = Blueprint("dashboard", __name__)

POLL_STALE_AFTER_S = 300
"""Give up dashboard polling five minutes after the shell queued the
probes. Normal probes resolve in seconds (the worker-side salt-api
timeout is 8s), so anything older means the worker died mid-probe and
the spinner would otherwise spin forever. An expired poll renders its
final state — whatever resolved plus snapshot — with no banner and no
further polling."""


def get_salt() -> SaltClient:
    return current_app.extensions["salt_client"]


def snapshot_versions(allowed: set[str] | None = None) -> dict[str, int]:
    """{saltversion: count} from the grain snapshot cache. Fallback
    when the master won't answer manage.versions. ``allowed`` restricts
    the count to in-scope minions (scoped RBAC)."""
    counts: dict[str, int] = {}
    # Grains column only: full rows would drag every minion's grains JSON.
    query = get_session().query(Minion.id, Minion.grains)
    if allowed is not None:
        if not allowed:
            return counts
        query = query.filter(Minion.id.in_(allowed))
    for _mid, grains in query.all():
        version = (grains or {}).get("saltversion")
        if version:
            version = str(version)
            counts[version] = counts.get(version, 0) + 1
    return counts


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _age_label(seconds: float) -> str:
    """Short relative age for the returns row. Pure data, unit-tested."""
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def returns_health(now: dt.datetime | None = None) -> dict:
    """Returner freshness: newest stored return vs newest completed job.

    ``stale`` is True only when jobs completed but no return covers
    them — the dead-returner signature. A quiet fleet with nothing run
    yet reads fresh with ``age`` None, so silence alone never alarms.
    """
    session = get_session()
    moment = now or dt.datetime.now(dt.UTC)
    last = _aware(session.query(func.max(SaltReturn.alter_time)).scalar())
    newest_done = _aware(
        session.query(func.max(Job.started_at)).filter_by(complete=True).scalar()
    )
    if last is None:
        stale = newest_done is not None
        age = None
    else:
        stale = newest_done is not None and newest_done > last
        age = _age_label((moment - last).total_seconds())
    return {"age": age, "stale": stale}


def snapshot_stats(
    allowed_minions: set[str] | None = None,
    job_mids: set[str] | None = None,
) -> dict:
    """Database-only dashboard numbers. No Salt I/O, so the shell and
    the poll endpoint serve it on every load while live panels resolve
    in the background. ``allowed_minions``/``job_mids`` restrict the
    counts to the caller's RBAC scope (None means the whole fleet)."""
    return {
        "reachable": False,
        "accepted": 0,
        "pending": 0,
        "keys_live": False,
        "up": 0,
        "down": 0,
        "presence_live": False,
        "in_flight": in_flight_db_count(job_mids),
        "in_flight_live": False,
        "versions": snapshot_versions(allowed_minions),
        "versions_live": False,
        "last_failures": last_failure_returns(limit=5, allowed=job_mids),
        "returns": returns_health(),
    }


def empty_stats() -> dict:
    """Zero-grant shell: the same keys as :func:`snapshot_stats`, all
    zeroed. No counts, no probes, no polling."""
    return {
        "reachable": False,
        "accepted": 0,
        "pending": 0,
        "keys_live": False,
        "up": 0,
        "down": 0,
        "presence_live": False,
        "in_flight": 0,
        "in_flight_live": False,
        "versions": {},
        "versions_live": False,
        "last_failures": [],
        "returns": {"stale": False, "last_ingest": None, "recent_minions": 0},
    }


def in_flight_db_count(job_mids: set[str] | None = None) -> int:
    """Incomplete jobs. ``job_mids`` restricts to jobs published into
    the caller's job.read scope (None means the whole fleet)."""
    from .authz import published_snapshot_ids

    query = get_session().query(Job).filter_by(complete=False)
    if job_mids is None:
        return query.count()
    count = 0
    for job in query.all():
        mids = published_snapshot_ids(job.tgt, job.tgt_type)
        if mids is None or mids & job_mids:
            count += 1
    return count


def last_failure_returns(limit: int = 5, allowed: set[str] | None = None) -> list:
    query = get_session().query(JobReturn).filter_by(success=False)
    if allowed is not None:
        if not allowed:
            return []
        query = query.filter(JobReturn.minion_id.in_(allowed))
    return query.order_by(JobReturn.id.desc()).limit(limit).all()


def collect_stats(
    client: SaltClient,
    keys: dict | None = None,
    presence: dict | None = None,
    versions: dict | None = None,
    allowed_minions: set[str] | None = None,
    job_mids: set[str] | None = None,
) -> dict:
    """Snapshot numbers plus any live results handed over (RQ job
    results or tests). Never calls Salt itself: live data arrives
    through the arguments. ``client`` is accepted for backward
    compatibility with existing callers. Each panel merges
    independently, so a dead minion stalling the fan-out panels
    (presence, versions) no longer blanks the master-local keys panel.
    """
    stats = snapshot_stats(allowed_minions, job_mids)
    if keys is not None or presence is not None or versions is not None:
        stats["reachable"] = True
    incomplete = {
        r[0] for r in get_session().query(Job.jid).filter_by(complete=False).all()
    }
    if keys is not None:
        stats["accepted"] = keys.get("accepted", 0)
        stats["pending"] = keys.get("pending", 0)
        stats["keys_live"] = True
        if keys.get("active_live"):
            stats["in_flight"] = len(incomplete & set(keys.get("active_jids") or []))
            stats["in_flight_live"] = True
    if presence is not None:
        stats["up"] = presence.get("up", 0)
        stats["down"] = presence.get("down", 0)
        stats["presence_live"] = True
    if versions is not None and versions.get("versions"):
        stats["versions"] = versions["versions"]
        stats["versions_live"] = True
    return stats


@bp.route("/")
@login_required
def index():
    """Dashboard shell: enqueue live probes, render snapshot instantly.

    No Salt I/O happens here — panels hydrate through :func:`panels`
    below, so a slow or sick master can never pin a worker. Each panel
    resolves to live data, or keeps the snapshot fallback it already
    shows, which also terminates its polling.
    """
    from .authz import has_any_access, has_fleet, rbac_mode
    from .tasks import (
        capabilities_task,
        fleet_keys_task,
        fleet_presence_task,
        fleet_versions_task,
        master_status_task,
        queue_or_none,
        read_capability_cache,
    )

    scoped = rbac_mode() == "scoped"
    # Zero-grant users get an empty shell: no probes, no polling, no
    # counts. Everyone else sees at least their own scoped numbers.
    empty = scoped and not has_any_access(current_user)
    fleet_keys = not scoped or has_fleet(current_user, "key.read")
    fleet_minions = not scoped or has_fleet(current_user, "minion.read")
    probe = not scoped or has_fleet(current_user, "dashboard.probe")
    keys_job = queue_or_none(fleet_keys_task) if fleet_keys and not empty else None
    presence_job = queue_or_none(fleet_presence_task) if fleet_minions and not empty else None
    versions_job = queue_or_none(fleet_versions_task) if fleet_minions and not empty else None
    masters_job = queue_or_none(master_status_task) if probe and not empty else None
    # Always probe: wheel/runner/history doors are meaningful before any
    # minion exists to ping, and the ping door skips itself on None.
    target = ping_target() if probe and not empty else None
    caps_job = queue_or_none(capabilities_task, target) if probe and not empty else None
    panels = {
        "keys": keys_job.id if keys_job is not None else None,
        "presence": presence_job.id if presence_job is not None else None,
        "versions": versions_job.id if versions_job is not None else None,
        "caps": caps_job.id if caps_job is not None else None,
        "masters": masters_job.id if masters_job is not None else None,
    }
    from .authz import minions_with

    client = get_salt()
    if empty:
        stats = empty_stats()
        cached = None
    else:
        allowed = job_mids = None
        if scoped and not fleet_minions:
            allowed = minions_with(current_user, "minion.read")
            job_mids = minions_with(current_user, "job.read")
        stats = collect_stats(client, allowed_minions=allowed, job_mids=job_mids)
        cached = read_capability_cache() if probe else None
    health, checks = build_health(
        client, stats["reachable"], cached, probing=any(panels.values())
    )
    return render_template(
        "dashboard.html",
        stats=stats,
        health=health,
        checks=checks,
        masters=None,
        panels=panels,
        poll_qs=_poll_qs(panels, _fingerprint({}, cached, stats), int(time.time())),
        probing=any(panels.values()),
        worker_down=not any(panels.values()),
    )


@bp.post("/dashboard/check-now")
@roles_required("operator")
def check_now():
    """Operator self-check: re-run the capability probes now. The panel
    picks the fresh result up through the normal poll path; when no
    worker answers, the cached check stays put and the page says so."""
    from .authz import require
    from .tasks import capabilities_task, queue_or_none

    require("dashboard.probe")
    job = queue_or_none(capabilities_task, ping_target())
    if job is None:
        flash("Worker unreachable. Showing the last cached check.", "warning")
        log_event(current_user.username, "capabilities-recheck:refused-offline")
    else:
        flash("Capability check queued. The panel updates live.", "info")
        log_event(current_user.username, "capabilities-recheck:queued")
    return redirect(url_for("dashboard.index"))


@bp.get("/dashboard/panels")
@login_required
def panels():
    """Live fragments for dashboard polling. Reads finished RQ results
    and re-renders the live region; never touches Salt, so polling a
    sick master stays cheap. Panels whose jobs died, expired, or were
    never queued resolve to snapshot data, which stops their polling."""
    from flask import abort

    from .authz import audit_deny, has_any_access, has_fleet, minions_with, rbac_mode
    from .tasks import read_capability_cache

    # Any grant admits the poll shell; zero-grant users get 403. Live
    # fleet probes hydrate only for holders of the matching fleet grant,
    # and capability results only for dashboard.probe holders.
    scoped = rbac_mode() == "scoped"
    if scoped and not has_any_access(current_user):
        audit_deny("dashboard.panels", detail="no-grants")
        abort(403)
    client = get_salt()
    jids = {
        key: request.args.get(key) or None
        for key in ("keys", "presence", "versions", "caps", "masters")
    }
    probe = not scoped or has_fleet(current_user, "dashboard.probe")
    fleet_keys = not scoped or has_fleet(current_user, "key.read")
    fleet_minions = not scoped or has_fleet(current_user, "minion.read")
    if scoped and not probe:
        jids["caps"] = jids["masters"] = None
    if scoped and not fleet_keys:
        jids["keys"] = None
    if scoped and not fleet_minions:
        jids["presence"] = jids["versions"] = None
    live: dict[str, Any] = {}
    probing = False
    for key, jid in jids.items():
        state, value = describe_job(jid)
        if state == "ready":
            live[key] = value
        elif state == "waiting":
            probing = True
    allowed = job_mids = None
    if scoped and not fleet_minions:
        allowed = minions_with(current_user, "minion.read")
        job_mids = minions_with(current_user, "job.read")
    stats = collect_stats(
        client,
        keys=live.get("keys"),
        presence=live.get("presence"),
        versions=live.get("versions"),
        allowed_minions=allowed,
        job_mids=job_mids,
    )
    caps = live.get("caps")
    if caps is None and probe:
        caps = read_capability_cache()
    masters = live.get("masters")
    fingerprint = _fingerprint(live, caps, stats)
    if probing and _poll_expired(request.args.get("started")):
        # Worker died mid-probe: keep whatever resolved, fall back the
        # rest to snapshot, and stop polling instead of spinning forever.
        probing = False
    if probing and fingerprint == request.args.get("seen", ""):
        # Nothing changed since the client's last render: answer 204 so
        # htmx swaps nothing and the spinner keeps spinning instead of
        # restarting on an identical re-render.
        return ("", 204)
    health, checks = build_health(
        client, stats["reachable"], caps, probing=probing and caps is None
    )
    return render_template(
        "_dashboard_live.html",
        stats=stats,
        health=health,
        checks=checks,
        masters=masters,
        probing=probing,
        worker_down=False,
        poll_qs=(
            _poll_qs(jids, fingerprint, _poll_started(request.args.get("started")))
            if probing
            else None
        ),
    )


def _fingerprint(live: dict[str, Any], caps: dict | None, stats: dict) -> str:
    """What the client already shows: ready panel keys, whether caps
    rendered (job result or cache), whether the masters probe resolved,
    and the DB counts that paint while probing — so a re-poll only
    re-renders on a visible change."""
    shown = set(live)
    if caps is not None:
        shown.add("caps")
    parts = sorted(shown)
    parts.append(f"masters-{bool(live.get('masters'))}")
    parts.append(f"in-flight-{stats['in_flight']}")
    parts.append(f"failures-{len(stats['last_failures'])}")
    parts.append(f"returns-stale-{stats['returns']['stale']}")
    return ",".join(parts)


def _poll_started(raw: str | None) -> int:
    """Poll-clock from the query string; missing or junk means the poll
    just started, so a bare panels URL always renders instead of 204ing
    or expiring on its first hit."""
    try:
        return int(raw or 0) or int(time.time())
    except (TypeError, ValueError):
        return int(time.time())


def _poll_expired(raw: str | None) -> bool:
    return time.time() - _poll_started(raw) > POLL_STALE_AFTER_S


def _poll_qs(
    panels: dict[str, str | None], seen: str | None = None, started: int | None = None
) -> str | None:
    parts = [f"{key}={jid}" for key, jid in panels.items() if jid]
    if not parts:
        return None
    if seen:
        parts.append(f"seen={seen}")
    if started is not None:
        parts.append(f"started={started}")
    return "&".join(parts)


def build_health(
    client: SaltClient, reachable: bool, caps: dict | None, probing: bool
) -> tuple[dict, list | None]:
    """Health panel + capability rows for a live-region render. ``caps``
    None while probing renders a "checking" state; None afterwards
    means the probe never produced data."""
    if caps is None:
        health = {
            "url": client.base_url,
            "token_age": client.token_age,
            "wheel_ok": None,
            "runner_ok": None,
            "reachable": reachable,
            "error": None,
        }
        return (health, None if probing else [])
    health = {
        "url": client.base_url,
        "token_age": client.token_age,
        "wheel_ok": caps["wheel_ok"],
        "runner_ok": caps["runner_ok"],
        "reachable": reachable,
        "error": caps.get("error"),
    }
    return (health, capability_checks(caps))


def ping_target() -> str | None:
    """One accepted minion for the execution-door probe, or None."""
    row = get_session().query(Minion.id).order_by(Minion.id).first()
    return row[0] if row else None


def capability_checks(caps: dict) -> list:
    """CAPABILITY_CHECKS annotated with cached probe results. A failed
    ping probe with no target means there was nothing to ping yet, not
    missing grants — say so instead of sending the reader to eauth."""
    from .tasks import CAPABILITY_CHECKS

    checks = []
    for c in CAPABILITY_CHECKS:
        check = {**c, "ok": bool(caps.get(c["key"]))}
        if c["key"] == "ping_ok" and not check["ok"] and not caps.get("ping_target"):
            check["grant"] = "No minion to ping yet — enroll one first"
        checks.append(check)
    return checks

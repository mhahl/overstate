"""Dashboard: snapshot shell rendered instantly, live panels hydrated
by polling. No Salt I/O happens in these request paths — only fast DB
reads and Redis job lookups — so a slow or sick master can never pin a
gunicorn worker. History always comes from Postgres."""

import time
from typing import Any

from flask import Blueprint, current_app, render_template, request
from flask_login import login_required

from .db import get_session
from .models import Job, JobReturn, Minion
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


def snapshot_versions() -> dict[str, int]:
    """{saltversion: count} from the grain snapshot cache. Fallback
    when the master won't answer manage.versions."""
    counts: dict[str, int] = {}
    # Grains column only: full rows would drag every minion's grains JSON.
    for (grains,) in get_session().query(Minion.grains).all():
        version = (grains or {}).get("saltversion")
        if version:
            version = str(version)
            counts[version] = counts.get(version, 0) + 1
    return counts


def snapshot_stats() -> dict:
    """Database-only dashboard numbers. No Salt I/O, so the shell and
    the poll endpoint serve it on every load while live panels resolve
    in the background."""
    return {
        "reachable": False,
        "accepted": 0,
        "pending": 0,
        "keys_live": False,
        "up": 0,
        "down": 0,
        "presence_live": False,
        "in_flight": in_flight_db_count(),
        "in_flight_live": False,
        "versions": snapshot_versions(),
        "versions_live": False,
        "last_failures": last_failure_returns(limit=5),
    }


def in_flight_db_count() -> int:
    return get_session().query(Job.jid).filter_by(complete=False).count()


def last_failure_returns(limit: int = 5) -> list:
    return (
        get_session()
        .query(JobReturn)
        .filter_by(success=False)
        .order_by(JobReturn.id.desc())
        .limit(limit)
        .all()
    )


def collect_stats(
    client: SaltClient,
    keys: dict | None = None,
    presence: dict | None = None,
    versions: dict | None = None,
) -> dict:
    """Snapshot numbers plus any live results handed over (RQ job
    results or tests). Never calls Salt itself: live data arrives
    through the arguments. ``client`` is accepted for backward
    compatibility with existing callers. Each panel merges
    independently, so a dead minion stalling the fan-out panels
    (presence, versions) no longer blanks the master-local keys panel.
    """
    stats = snapshot_stats()
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
    from .tasks import (
        capabilities_task,
        fleet_keys_task,
        fleet_presence_task,
        fleet_versions_task,
        master_status_task,
        queue_or_none,
        read_capability_cache,
    )

    keys_job = queue_or_none(fleet_keys_task)
    presence_job = queue_or_none(fleet_presence_task)
    versions_job = queue_or_none(fleet_versions_task)
    masters_job = queue_or_none(master_status_task)
    # Always probe: wheel/runner/history doors are meaningful before any
    # minion exists to ping, and the ping door skips itself on None.
    target = ping_target()
    caps_job = queue_or_none(capabilities_task, target)
    panels = {
        "keys": keys_job.id if keys_job is not None else None,
        "presence": presence_job.id if presence_job is not None else None,
        "versions": versions_job.id if versions_job is not None else None,
        "caps": caps_job.id if caps_job is not None else None,
        "masters": masters_job.id if masters_job is not None else None,
    }
    client = get_salt()
    stats = collect_stats(client)
    cached = read_capability_cache()
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


@bp.get("/dashboard/panels")
@login_required
def panels():
    """Live fragments for dashboard polling. Reads finished RQ results
    and re-renders the live region; never touches Salt, so polling a
    sick master stays cheap. Panels whose jobs died, expired, or were
    never queued resolve to snapshot data, which stops their polling."""
    from .tasks import read_capability_cache

    client = get_salt()
    jids = {
        key: request.args.get(key) or None
        for key in ("keys", "presence", "versions", "caps", "masters")
    }
    live: dict[str, Any] = {}
    probing = False
    for key, jid in jids.items():
        state, value = describe_job(jid)
        if state == "ready":
            live[key] = value
        elif state == "waiting":
            probing = True
    stats = collect_stats(
        client,
        keys=live.get("keys"),
        presence=live.get("presence"),
        versions=live.get("versions"),
    )
    caps = live.get("caps")
    if caps is None:
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

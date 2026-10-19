"""Vulnerabilities: fleet CVE summaries and per-minion detail.

Read-only over the grype mine keys through salt-api only: the global
page fans out the slim ``grype.scan_summary`` key (counts/total/top),
the minion vuln tab reads the full ``grype.scan_detail`` key with
fallback to the old combined summary format. The app never reads the
minion-data Redis directly; pagination, search, and sorting run in the
view over each salt-api response, so every column sorts globally.

``mine.update`` does not refresh either key: both live only as long as
the daily ``grype-cve-scan`` schedule refreshes them. Empty states say
so instead of pointing at the generic mine-refresh preset.
"""

from __future__ import annotations

import datetime as dt

import httpx
from flask import Blueprint, render_template, request
from flask_login import login_required

from .dashboard import get_salt, ping_target
from .salt_client import SaltApiError
from .tasks import mine_get_now, mine_get_task, queue_or_none, wait_for

bp = Blueprint("vulns", __name__, url_prefix="/vulnerabilities")

SUMMARY_KEY = "grype.scan_summary"
DETAIL_KEY = "grype.scan_detail"

SEVERITY_RANK = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "NEGLIGIBLE": 4,
    "UNKNOWN": 5,
}
SORT_COLUMNS = ("minion", "total", "fixable", "scanned_at")
# A summary older than this many days is flagged as a stale scan.
VULN_STALE_DAYS = 7


def scan_age_days(scanned_at: str | None) -> int | None:
    """Whole days between now and a scan timestamp, or None when absent."""
    if not scanned_at:
        return None
    try:
        when = dt.datetime.fromisoformat(scanned_at)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0, int((dt.datetime.now(dt.UTC) - when).total_seconds() // 86400))


SNAPSHOT_LIMIT = 10


def _snapshot_cves(entries: dict) -> dict[str, list[str]]:
    """Per-minion CVE lists from the summary top lists (best effort)."""
    out: dict[str, list[str]] = {}
    for mid, value in entries.items():
        if not isinstance(value, dict):
            continue
        top = value.get("top")
        if not isinstance(top, list):
            continue
        cves = [
            str(f.get("cve"))
            for f in top
            if isinstance(f, dict) and f.get("cve")
        ]
        out[mid] = sorted(set(cves))
    return out


def record_snapshot(entries: dict) -> dict[str, int]:
    """Diff current top-list CVEs against the previous snapshot, then
    record the new one. Returns {mid: new_cve_count}; never raises."""
    from .db import get_session
    from .models import VulnSnapshot

    try:
        session = get_session()
        current = _snapshot_cves(entries)
        previous = session.query(VulnSnapshot).order_by(VulnSnapshot.id.desc()).first()
        prev_cves = (
            previous.cves
            if previous is not None and isinstance(previous.cves, dict)
            else {}
        )
        # No baseline yet: the first snapshot only establishes one, so
        # the whole fleet does not light up "+N new" on the first load.
        new_by_minion: dict[str, int] = {}
        if previous is not None:
            new_by_minion = {
                mid: len(set(cves) - set(prev_cves.get(mid, [])))
                for mid, cves in current.items()
                if set(cves) - set(prev_cves.get(mid, []))
            }
        session.add(VulnSnapshot(cves=current))
        session.flush()
        for stale in (
            session.query(VulnSnapshot)
            .order_by(VulnSnapshot.id.desc())
            .offset(SNAPSHOT_LIMIT)
            .all()
        ):
            session.delete(stale)
        session.commit()
        return new_by_minion
    except Exception:  # noqa: BLE001 — the snapshot never blocks the page
        return {}


def summarize_entries(entries: dict) -> tuple[list[dict], list[str]]:
    """Normalize fleet summaries into rows plus rank-ordered severities.

    Returns (rows, severities). A row holds mid, counts, total,
    fixable, scanned_at, and either an error string or None. Values
    that are not mappings (or lack counts) become error/empty rows so
    one malformed minion never breaks the page.
    """
    severities: set[str] = set()
    rows: list[dict] = []
    for mid, value in entries.items():
        row: dict = {"mid": mid, "error": None}
        if isinstance(value, dict) and isinstance(value.get("counts"), dict):
            counts = {
                str(sev).upper(): int(n)
                for sev, n in value["counts"].items()
                if isinstance(n, (int, float))
            }
            row.update(
                counts=counts,
                total=int(value.get("total", 0) or 0),
                fixable=int(value.get("fixable", 0) or 0),
                scanned_at=str(value.get("scanned_at") or ""),
            )
            severities.update(counts)
        elif isinstance(value, dict) and value.get("error"):
            row.update(
                counts={},
                total=0,
                fixable=0,
                scanned_at=str(value.get("scanned_at") or ""),
                error=str(value["error"]),
            )
        else:
            row.update(counts={}, total=0, fixable=0, scanned_at="")
        rows.append(row)
    ordered = sorted(severities, key=lambda s: (SEVERITY_RANK.get(s, 99), s))
    return rows, ordered


def sort_rows(rows: list[dict], sort: str, direction: str) -> list[dict]:
    """Sort the full row set (globally, before pagination)."""
    reverse = direction != "asc"

    def key(row: dict):
        if sort == "minion":
            return (row["mid"],)
        if sort == "scanned_at":
            return (row["scanned_at"], row["mid"])
        if sort in ("total", "fixable"):
            return (row[sort], row["mid"])
        return (row["counts"].get(sort, 0), row["mid"])

    if sort == "minion":
        return sorted(rows, key=key, reverse=(direction == "desc"))
    return sorted(rows, key=key, reverse=reverse)


@bp.route("/cve/<cve>")
@login_required
def cve(cve: str):
    """Which minions currently report a specific CVE.

    Reads the latest fleet snapshot (see ``record_snapshot``) rather
    than fanning out live detail keys, so it is cheap and consistent
    with what the fleet page last saw.
    """
    from flask_login import current_user

    from .authz import minions_with, rbac_mode, require
    from .db import get_session
    from .models import VulnSnapshot

    if rbac_mode() == "scoped":
        require("mine.read")
    snap = (
        get_session()
        .query(VulnSnapshot)
        .order_by(VulnSnapshot.id.desc())
        .first()
    )
    affected: list[str] = []
    if snap is not None and isinstance(snap.cves, dict):
        affected = sorted(m for m, cves in snap.cves.items() if cve in cves)
    if rbac_mode() == "scoped":
        allowed = minions_with(current_user, "mine.read")
        affected = [m for m in affected if m in allowed]
    return render_template("vulns_cve.html", cve=cve, affected=affected)


@bp.route("/")
@login_required
def index():
    from flask import abort
    from flask_login import current_user

    from .authz import (
        AuthzDenied,
        audit_deny,
        constrain_target_verbose,
        rbac_mode,
        require,
    )

    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "total")
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    scoped = rbac_mode() == "scoped"
    entries: dict = {}
    error = None
    hit: set[str] | None = None
    if scoped:
        require("mine.read")
        try:
            _, _, _, hit = constrain_target_verbose(
                current_user, "mine.read", "*", "glob"
            )
        except AuthzDenied as exc:
            audit_deny(exc.perm, detail=exc.detail)
            abort(403)
        reader = min(hit) if hit is not None else ping_target()
        if reader is None:
            error = "No minions in the snapshot cache."
    else:
        reader = ping_target()
        if reader is None:
            error = "No minions in the snapshot cache."
    if error is None:
        try:
            queued = queue_or_none(mine_get_task, reader, "*", SUMMARY_KEY, "glob")
            if queued is None:
                entries = mine_get_now(get_salt(), reader, "*", SUMMARY_KEY, "glob")
            else:
                status, value = wait_for(queued, wait=6.0)
                if status == "ready":
                    entries = value
                elif status == "pending":
                    error = "Vulnerability query still running. Reload to retry."
                else:
                    error = f"Vulnerability query failed: {value}"
        except (SaltApiError, httpx.HTTPError) as exc:
            error = f"salt-api error: {exc}"
    if not isinstance(entries, dict):
        entries = {}
    if scoped and hit is not None:
        entries = {k: v for k, v in entries.items() if k in hit}
    rows, severities = summarize_entries(entries)
    new_by_minion = record_snapshot(entries)
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in r["mid"].lower()]
    allowed = set(SORT_COLUMNS) | set(severities)
    if sort not in allowed:
        sort, direction = "total", "desc"
    rows = sort_rows(rows, sort, direction)
    # Fleet rollup + staleness: computed over the full (unpaged) set.
    totals = {
        "critical": sum(r["counts"].get("CRITICAL", 0) for r in rows if not r["error"]),
        "high": sum(r["counts"].get("HIGH", 0) for r in rows if not r["error"]),
        "fixable": sum(r["fixable"] for r in rows if not r["error"]),
        "total": sum(r["total"] for r in rows if not r["error"]),
    }
    oldest = max((scan_age_days(r["scanned_at"]) for r in rows if not r["error"]), default=None)
    stale = {
        r["mid"] for r in rows if (scan_age_days(r["scanned_at"]) or 0) > VULN_STALE_DAYS
    }
    from .minions import resolve_page_size

    per_page = resolve_page_size(request.args.get("per_page"))
    try:
        requested_page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        requested_page = 1
    total = len(rows)
    pages = max(1, -(-total // per_page))
    page = min(requested_page, pages)
    rows = rows[(page - 1) * per_page : page * per_page]
    return render_template(
        "vulns.html",
        rows=rows,
        severities=severities,
        q=q,
        sort=sort,
        direction=direction,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        error=error,
        totals=totals,
        stale=stale,
        stale_days=VULN_STALE_DAYS,
        oldest_scan_days=oldest,
        new_by_minion=new_by_minion,
    )

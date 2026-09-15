"""Watched states + per-minion conformity from the last highstate-style job."""

import datetime as dt

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .db import get_session
from .models import Job, JobReturn, Minion, StateConformityHistory, WatchedState

bp = Blueprint("states", __name__, url_prefix="/states")

HISTORY_LIMIT = 20


def _narrow_to_watched(payload, watched: list[str]) -> tuple[str | None, bool]:
    """Restrict a state return payload to the watched SLS files.

    Returns (status, partial). (None, True) means the payload cannot be
    narrowed — not a mapping, empty, or no entry belongs to a watched
    SLS (e.g. an unknown watch name) — so the caller falls back to the
    whole-job verdict and surfaces ``partial``. Degrades, never blocks.
    """
    if not isinstance(payload, dict) or not payload:
        return None, True
    wanted = set(watched)
    matched = {
        sid: st
        for sid, st in payload.items()
        if isinstance(st, dict) and st.get("__sls__") in wanted
    }
    if not matched:
        return None, True
    for st in matched.values():
        result = st.get("result")
        if result is False or (result is None and bool(st.get("changes"))):
            return "drifted", False
    return "ok", False


def _watched_list(session) -> list[str]:
    return [w.sls for w in session.query(WatchedState).order_by(WatchedState.sls)]


def _record_history(session, mid: str, jid: str, status: str, now: dt.datetime) -> None:
    """Append one verdict-trail row, pruning to the newest HISTORY_LIMIT."""
    session.add(
        StateConformityHistory(minion_id=mid, jid=jid, status=status, checked_at=now)
    )
    session.flush()
    stale = (
        session.query(StateConformityHistory)
        .filter_by(minion_id=mid)
        .order_by(StateConformityHistory.id.desc())
        .offset(HISTORY_LIMIT)
        .all()
    )
    for row in stale:
        session.delete(row)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def apply_sync_verdicts(job: Job, rows: list) -> None:
    """Stamp conformity from returner rows as a state job lands.

    Called by ``sync_job()`` so verdicts stay current without a manual
    Recompute. Only minions with a return in this job — or provably
    targeted by it once the job has aged out — are touched, so a job
    whose target never covered a minion can never claim to have
    checked it. A verdict stamped by a newer job is never overwritten
    by an older job completing out of order.
    """
    if job is None or not (job.fun or "").startswith("state."):
        return
    session = get_session()
    now = dt.datetime.now(dt.UTC)
    returns_by_minion = {}
    for r in rows or []:
        if r.jid == job.jid:
            returns_by_minion[r.minion_id] = r
    try:
        from .jobs_service import resolve_batch_roster

        roster = resolve_batch_roster(job.tgt or "", job.tgt_type or "glob")
    except Exception:  # noqa: BLE001 — unknown group: skip unreachable marks
        roster = None
    touched = set(returns_by_minion)
    if job.complete and roster:
        touched |= set(roster)
    watched = _watched_list(session)
    for mid in sorted(touched):
        ret = returns_by_minion.get(mid)
        partial = False
        if ret is not None:
            payload = ret.payload if isinstance(ret.payload, dict) else {}
            if watched:
                narrowed, partial = _narrow_to_watched(payload, watched)
                if narrowed is not None:
                    status = narrowed
                else:
                    status = "ok" if str(ret.success).lower() == "true" else "drifted"
            else:
                status = "ok" if str(ret.success).lower() == "true" else "drifted"
        elif job.complete and roster is not None and mid in roster:
            status = "unreachable"
        else:
            continue
        row = session.get(Minion, mid)
        if row is not None:
            ejid = (row.conformity or {}).get("jid")
            if ejid and ejid != job.jid:
                ejob = session.get(Job, ejid)
                if (
                    ejob is not None
                    and ejob.started_at is not None
                    and job.started_at is not None
                    and _aware(ejob.started_at) > _aware(job.started_at)
                ):
                    continue  # a newer job already has the floor
        else:
            row = Minion(id=mid, grains={}, conformity={})
            session.add(row)
        row.conformity = {
            "status": status,
            "jid": job.jid,
            "checked_at": now.isoformat(),
            "targeted": True,
        }
        if partial:
            row.conformity["partial"] = True
        _record_history(session, mid, job.jid, status, now)


def recompute_conformity() -> str | None:
    """Derive ok/drifted per minion from the most recently active state.*
    job. Returns the source jid (or None). Only minions with a return
    in that job are updated; everyone else keeps their prior verdict so
    a job whose target glob does not cover a minion never claims to
    have checked it."""
    session = get_session()
    latest = (
        session.query(Job.jid)
        .filter(Job.fun.like("state.%"))
        .order_by(Job.started_at.desc())
        .first()
    )
    if not latest:
        return None
    jid = latest[0]
    if not session.query(JobReturn).filter_by(jid=jid).first():
        return None
    now = dt.datetime.now(dt.UTC)
    watched = _watched_list(session)
    for ret in session.query(JobReturn).filter_by(jid=jid).all():
        row = session.get(Minion, ret.minion_id)
        if row is None:
            row = Minion(id=ret.minion_id, grains={}, conformity={})
            session.add(row)
        payload = ret.payload if isinstance(ret.payload, dict) else {}
        if watched:
            narrowed, _ = _narrow_to_watched(payload, watched)
            status = (
                narrowed
                if narrowed is not None
                else ("ok" if ret.success else "drifted")
            )
        else:
            status = "ok" if ret.success else "drifted"
        row.conformity = {"status": status, "jid": jid}
        _record_history(session, ret.minion_id, jid, status, now)
    session.commit()
    return jid


CONFORMITY_ORDER = {"ok": 0, "drifted": 1, "unreachable": 2, "unknown": 3}


@bp.route("/")
@login_required
def index():
    session = get_session()
    watched = session.query(WatchedState).order_by(WatchedState.sls).all()
    minions = session.query(Minion).order_by(Minion.id).all()
    sort = request.args.get("sort", "id")
    if sort not in ("id", "status"):
        sort = "id"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    status_filter = request.args.get("status", "")
    if status_filter not in ("ok", "drifted", "unknown", "unreachable"):
        status_filter = ""

    def key(row):
        status = (row.conformity or {}).get("status", "unknown")
        if sort == "status":
            return (CONFORMITY_ORDER.get(status, 2), row.id)
        return row.id

    ordered = sorted(minions, key=key, reverse=(direction == "desc"))
    if status_filter:
        ordered = [
            row
            for row in ordered
            if (row.conformity or {}).get("status", "unknown") == status_filter
        ]
    history: dict[str, list[dict]] = {}
    if ordered:
        ids = [row.id for row in ordered]
        for trail in (
            session.query(StateConformityHistory)
            .filter(StateConformityHistory.minion_id.in_(ids))
            .order_by(StateConformityHistory.id.desc())
            .all()
        ):
            entries = history.setdefault(trail.minion_id, [])
            if len(entries) < 5:
                entries.append({"status": trail.status, "jid": trail.jid})
    ctx = {
        "watched": watched,
        "minions": ordered,
        "sort": sort,
        "direction": direction,
        "status_filter": status_filter,
        "history": history,
    }
    if request.headers.get("HX-Request") == "true":
        return render_template("_conformity_rows.html", **ctx)
    return render_template("states.html", **ctx)


@bp.post("/watch")
@roles_required("operator")
def watch():
    sls = request.form.get("sls", "").strip()
    session = get_session()
    if not sls:
        flash("Enter a state name to watch.", "error")
    elif session.query(WatchedState).filter_by(sls=sls).first():
        flash(f"Already watching '{sls}'.", "info")
    else:
        session.add(WatchedState(sls=sls))
        session.commit()
        log_event(current_user.username, f"watch:{sls}")
        flash(f"Watching '{sls}'.", "success")
    return redirect(url_for("states.index"))


@bp.post("/unwatch/<int:wid>")
@roles_required("operator")
def unwatch(wid: int):
    session = get_session()
    row = session.get(WatchedState, wid)
    if row is None:
        flash("Nothing to unwatch.", "error")
    else:
        flash(f"Stopped watching '{row.sls}'.", "success")
        log_event(current_user.username, f"unwatch:{row.sls}")
        session.delete(row)
        session.commit()
    return redirect(url_for("states.index"))


@bp.post("/recompute")
@roles_required("operator")
def recompute():
    jid = recompute_conformity()
    log_event(current_user.username, f"states-recompute:{jid or 'none'}")
    flash(f"Conformity recomputed from {jid}." if jid else "No state jobs yet.", "info")
    return redirect(url_for("states.index"))

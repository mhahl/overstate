"""Watched states + per-minion conformity from the last highstate-style job."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from .auth import roles_required
from .db import get_session
from .models import Job, JobReturn, Minion, WatchedState

bp = Blueprint("states", __name__, url_prefix="/states")


def recompute_conformity() -> str | None:
    """Derive ok/drifted per minion from the most recently active state.*
    job. Returns the source jid (or None). Unknown where no data exists."""
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
    seen: set[str] = set()
    for ret in session.query(JobReturn).filter_by(jid=jid).all():
        seen.add(ret.minion_id)
        row = session.get(Minion, ret.minion_id)
        if row is None:
            row = Minion(id=ret.minion_id, grains={}, conformity={})
            session.add(row)
        row.conformity = {"status": "ok" if ret.success else "drifted", "jid": jid}
    for row in session.query(Minion).all():
        if row.id not in seen:
            conf = dict(row.conformity or {})
            if conf.get("jid") != jid:
                conf.update({"status": "unknown", "jid": jid})
                row.conformity = conf
    session.commit()
    return jid


CONFORMITY_ORDER = {"ok": 0, "drifted": 1, "unknown": 2}


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

    def key(row):
        status = (row.conformity or {}).get("status", "unknown")
        if sort == "status":
            return (CONFORMITY_ORDER.get(status, 2), row.id)
        return row.id

    ordered = sorted(minions, key=key, reverse=(direction == "desc"))
    ctx = {"watched": watched, "minions": ordered, "sort": sort, "direction": direction}
    if request.headers.get("HX-Request") == "true":
        return render_template("_conformity_rows.html", **ctx)
    return render_template("states.html", **ctx)


@bp.post("/watch")
@roles_required("operator")
def watch():
    sls = request.form.get("sls", "").strip()
    session = get_session()
    if sls and not session.query(WatchedState).filter_by(sls=sls).first():
        session.add(WatchedState(sls=sls))
        session.commit()
        flash(f"Watching '{sls}'.", "success")
    return redirect(url_for("states.index"))


@bp.post("/unwatch/<int:wid>")
@roles_required("operator")
def unwatch(wid: int):
    session = get_session()
    row = session.get(WatchedState, wid)
    if row:
        session.delete(row)
        session.commit()
    return redirect(url_for("states.index"))


@bp.post("/recompute")
@roles_required("operator")
def recompute():
    jid = recompute_conformity()
    flash(f"Conformity recomputed from {jid}." if jid else "No state jobs yet.", "info")
    return redirect(url_for("states.index"))

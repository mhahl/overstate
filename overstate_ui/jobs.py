"""Job runner, history synced from the returner tables, saved jobs, SSE stream."""

from __future__ import annotations

import datetime as dt
import json
import time

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required
from .auth import roles_required

from .audit import log_event
from .dashboard import get_salt
from .db import get_session
from .models import Job, JobReturn, SaltReturn, SavedJob
from .salt_client import SaltApiError

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

TGT_TYPES = ["glob", "list", "grain", "compound", "nodegroup"]
COMPLETE_AFTER_SECONDS = 60
JOB_SORT_COLUMNS = ("started", "fun", "user")


def sort_jobs(rows: list, sort: str, direction: str) -> list:
    """Sort job rows in place. Started defaults to newest first."""
    reverse = direction == "desc"
    if sort == "fun":
        rows.sort(key=lambda j: (j.fun, j.jid), reverse=reverse)
    elif sort == "user":
        rows.sort(key=lambda j: (j.user, j.jid), reverse=reverse)
    else:
        def started_key(j):
            if isinstance(j.started_at, dt.datetime):
                return (j.started_at.timestamp(), j.jid)
            return (float("-inf"), j.jid)

        rows.sort(key=started_key, reverse=reverse)
    return rows


def suggest_glob(ids: list[str], roster: list[str]) -> tuple[str, list, list]:
    """Compile a minion selection to a glob target (D7). Returns
    (glob, selected ids, roster ids the glob covers) so the run form can
    show exactly what would be hit before firing."""
    import os

    selected = sorted({i.strip() for i in ids if i and i.strip()})
    roster = sorted(set(roster))
    if not selected:
        return "*", [], roster
    if roster and set(selected) >= set(roster):
        return "*", selected, roster
    if len(selected) == 1:
        only = selected[0]
        return only, selected, [only] if only in roster else []
    prefix = os.path.commonprefix(selected)
    if not prefix:
        return "*", selected, roster
    glob = prefix + "*"
    covered = sorted(m for m in roster if m.startswith(prefix))
    return glob, selected, covered

# Functions that change fleet state: the run form must carry a typed
# confirmation of the target before launch() fires.
DESTRUCTIVE_FUNS = frozenset({
    "pkg.install", "pkg.remove", "service.restart", "ps.kill_pid",
})

FLEET_PRESETS = {
    "pkg-install": {"tgt": "*", "tgt_type": "glob", "fun": "pkg.install",
                    "args": ""},
    "pkg-remove": {"tgt": "*", "tgt_type": "glob", "fun": "pkg.remove",
                   "args": ""},
    "service-restart": {"tgt": "*", "tgt_type": "glob",
                        "fun": "service.restart", "args": ""},
    "service-status": {"tgt": "*", "tgt_type": "glob",
                       "fun": "service.status", "args": ""},
    "process-signal": {"tgt": "*", "tgt_type": "glob", "fun": "ps.kill_pid",
                       "args": "<pid> <signal>"},
    "minion-restart": {"tgt": "*", "tgt_type": "glob",
                       "fun": "service.restart", "args": "salt-minion"},
    "mine-update": {"tgt": "*", "tgt_type": "glob", "fun": "mine.update",
                    "args": ""},
    "refresh-pillar": {"tgt": "*", "tgt_type": "glob",
                       "fun": "saltutil.refresh_pillar", "args": ""},
    "sync-all": {"tgt": "*", "tgt_type": "glob", "fun": "saltutil.sync_all",
                 "args": ""},
}


def sync_job(jid: str) -> Job | None:
    """Copy returner rows for jid into jobs/job_returns. Heuristic: a job is
    complete once returns exist and the youngest is older than
    COMPLETE_AFTER_SECONDS (Salt exposes no completion flag)."""
    session = get_session()
    rows = session.query(SaltReturn).filter_by(jid=jid).all()
    if not rows:
        return session.get(Job, jid)
    job = session.get(Job, jid)
    if job is None:
        first = rows[0]
        job = Job(jid=jid, fun=first.fun, tgt="", tgt_type="glob",
                  user=current_user.username
                  if current_user.is_authenticated else "unknown")
        session.add(job)
    for r in rows:
        existing = (
            session.query(JobReturn).filter_by(jid=jid, minion_id=r.minion_id).first()
        )
        success = str(r.success).lower() == "true"
        payload = r.payload if isinstance(r.payload, dict) else {}
        if existing is None:
            session.add(JobReturn(jid=jid, minion_id=r.minion_id,
                                  success=success, retcode=0, payload=payload))
        else:
            existing.success = success
            existing.payload = payload
    def aware(value: dt.datetime) -> dt.datetime:
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)

    youngest = max(
        (aware(r.alter_time) for r in rows if r.alter_time),
        default=None,
    )
    now = dt.datetime.now(dt.timezone.utc)
    if youngest is None:
        job.complete = (now - aware(job.started_at)).total_seconds() > COMPLETE_AFTER_SECONDS
    else:
        job.complete = (now - youngest).total_seconds() > COMPLETE_AFTER_SECONDS
    session.commit()
    return job


def launch(tgt: str, tgt_type: str, fun: str, args: list,
           asynchronous: bool, via: str = "local") -> str:
    """Fire a job via salt-api; record the Job row. Returns the jid."""
    client = get_salt()
    if via == "ssh":
        # salt-ssh has no async path: synchronous over the roster, returns
        # carry no JID, so the job is recorded complete with a synthetic one.
        client.local(tgt, fun, arg=args, tgt_type=tgt_type, timeout=180,
                     via="ssh")
        jid = f"ssh-{int(time.time())}"
        session = get_session()
        session.add(Job(jid=jid, fun=fun, tgt=tgt, tgt_type=tgt_type,
                        user=current_user.username, complete=True))
        session.commit()
        log_event(current_user.username, f"run-ssh:{fun}", jid=jid)
        return jid
    if asynchronous:
        result = client.local(tgt, fun, arg=args, tgt_type=tgt_type,
                              asynchronous=True)
        jid = result[0]["jid"] if isinstance(result, list) else result["jid"]
    else:
        result = client.local(tgt, fun, arg=args, tgt_type=tgt_type, timeout=60)
        jid = result[0].get("jid", "") if isinstance(result, list) else ""
        if not jid:  # sync calls may not surface a jid; synthesize one
            jid = f"sync-{int(time.time())}"
    session = get_session()
    session.add(Job(jid=jid, fun=fun, tgt=tgt, tgt_type=tgt_type,
                    user=current_user.username))
    session.commit()
    log_event(current_user.username, f"run:{fun}", jid=jid)
    return jid


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "running")
    if tab not in ("running", "history", "saved"):
        tab = "running"
    session = get_session()
    # Opportunistic sync so finished jobs land in history without opening
    # every detail page. Bounded to the 10 most recent running jobs.
    for job in session.query(Job).filter_by(complete=False).order_by(
            Job.started_at.desc()).limit(10).all():
        try:
            sync_job(job.jid)
        except (SaltApiError, ValueError):
            pass
    sort = request.args.get("sort", "started")
    if sort not in JOB_SORT_COLUMNS:
        sort = "started"
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    running = sort_jobs(session.query(Job).filter_by(complete=False).order_by(
        Job.started_at.desc()).all(), sort, direction)
    history = sort_jobs(session.query(Job).filter_by(complete=True).order_by(
        Job.started_at.desc()).limit(50).all(), sort, direction)
    saved = session.query(SavedJob).order_by(SavedJob.name).all()
    ctx = dict(tab=tab, running=running, history=history, saved=saved,
               sort=sort, direction=direction)
    if request.headers.get("HX-Request") == "true":
        return render_template("_job_rows.html", **ctx)
    return render_template("jobs.html", **ctx)


@bp.route("/new")
@login_required
def new():
    preset = request.args.get("preset", "")
    presets = {
        "ping": {"tgt": "*", "tgt_type": "glob", "fun": "test.ping", "args": ""},
        "highstate": {"tgt": "*", "tgt_type": "glob", "fun": "state.highstate",
                      "args": ""},
        "highstate-dry": {"tgt": "*", "tgt_type": "glob",
                          "fun": "state.highstate", "args": "test=True"},
        "apply": {"tgt": "*", "tgt_type": "glob", "fun": "state.apply",
                  "args": ""},
        **FLEET_PRESETS,
    }
    saved = None
    if request.args.get("saved"):
        saved = get_session().get(SavedJob, int(request.args["saved"]))
    preset = dict(presets.get(preset, {}))
    bulk = None
    raw_bulk = []
    for value in request.args.getlist("bulk"):
        raw_bulk.extend(value.split(","))
    if raw_bulk and not saved and not preset:
        from .models import Minion

        roster = [row.id for row in get_session().query(Minion.id).all()]
        glob, selected, covered = suggest_glob(raw_bulk, roster)
        preset = {"tgt": glob, "tgt_type": "glob"}
        bulk = {"ids": selected, "glob": glob, "covered": covered}
    bulk_ignored = bool(raw_bulk) and bulk is None
    if not saved and "tgt" not in preset:
        from .settings import get_setting

        preset["tgt"] = get_setting("default_target")
    return render_template("job_new.html", tgt_types=TGT_TYPES,
                           preset=preset, saved=saved, bulk=bulk,
                           bulk_ignored=bulk_ignored)


@bp.post("/run")
@roles_required("operator")
def run():
    tgt = request.form.get("tgt", "*").strip()
    tgt_type = request.form.get("tgt_type", "glob")
    fun = request.form.get("fun", "").strip()
    raw_args = request.form.get("args", "").strip()
    args = [a for a in raw_args.split() if a] if raw_args else []
    asynchronous = request.form.get("mode", "async") == "async"
    via = request.form.get("via", "local")
    if via not in ("local", "ssh"):
        via = "local"
    if not fun or tgt_type not in TGT_TYPES:
        flash("Target type and function are required.")
        return redirect(url_for("jobs.new"))
    if via == "ssh" and asynchronous:
        asynchronous = False
        flash("salt-ssh runs synchronously; switched to sync mode.")
    if fun in DESTRUCTIVE_FUNS and request.form.get("confirm", "") != tgt:
        return render_template("job_confirm.html", tgt=tgt, tgt_type=tgt_type,
                               fun=fun, raw_args=raw_args,
                               mode=request.form.get("mode", "async"),
                               via=via,
                               save_as=request.form.get("save_as", ""))
    try:
        jid = launch(tgt, tgt_type, fun, args, asynchronous, via=via)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}")
        return redirect(url_for("jobs.new"))
    if request.form.get("save_as"):
        session = get_session()
        session.add(SavedJob(name=request.form["save_as"], fun=fun, tgt=tgt,
                             tgt_type=tgt_type, args=args))
        session.commit()
    return redirect(url_for("jobs.detail", jid=jid))


@bp.route("/<jid>")
@login_required
def detail(jid: str):
    sync_job(jid)
    session = get_session()
    job = session.get(Job, jid)
    if job is None:
        flash("Unknown job.")
        return redirect(url_for("jobs.index", tab="history"))
    returns = session.query(JobReturn).filter_by(jid=jid).all()
    return render_template("job_detail.html", job=job, returns=returns)


@bp.post("/<jid>/sync")
@roles_required("operator")
def sync(jid: str):
    try:
        sync_job(jid)
    except (SaltApiError, ValueError) as exc:
        flash(f"sync error: {exc}")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/saved/<int:saved_id>/delete")
@roles_required("operator")
def delete_saved(saved_id: int):
    session = get_session()
    saved = session.get(SavedJob, saved_id)
    if saved:
        session.delete(saved)
        session.commit()
        flash(f"Deleted saved job '{saved.name}'.")
    return redirect(url_for("jobs.index", tab="saved"))


@bp.route("/<jid>/stream")
@login_required
def stream(jid: str):
    try:
        interval = max(0.05, min(5.0, float(request.args.get("interval", 2.0))))
    except ValueError:
        interval = 2.0

    def events():
        for _ in range(30):
            job = sync_job(jid)
            returns = (
                get_session().query(JobReturn).filter_by(jid=jid).all()
            )
            payload = {
                "jid": jid,
                "complete": bool(job and job.complete),
                "returned": len(returns),
                "failed": sum(1 for r in returns if not r.success),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if payload["complete"]:
                break
            time.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream")

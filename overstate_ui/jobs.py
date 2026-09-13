"""Job runner, history synced from the returner tables, saved jobs, SSE stream."""

from __future__ import annotations

import datetime as dt
import json
import re
import time

import httpx
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
from .dashboard import get_salt, ping_target
from .db import get_session
from .models import Job, JobReturn, SaltReturn, SavedJob
from .salt_client import SaltApiError

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

TGT_TYPES = ["glob", "list", "grain", "compound", "nodegroup", "group"]
COMPLETE_AFTER_SECONDS = 60
JOB_SORT_COLUMNS = ("started", "fun", "user")
FUN_RE = re.compile(r"^[A-Za-z0-9_.]+$")


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

# Functions that change fleet state: the run form must pass a one-click
# review (function, target, matched minions) before launch() fires.
DESTRUCTIVE_FUNS = frozenset({
    "pkg.install", "pkg.remove", "service.restart", "ps.kill_pid",
})

# Everything gated by the review modal: DESTRUCTIVE_FUNS plus the
# fleet-reconfiguring state runs. Test-mode state runs are exempt.
CONFIRM_FUNS = DESTRUCTIVE_FUNS | {"state.apply", "state.highstate"}


def is_test_mode(fun: str, args: list[str]) -> bool:
    """True for state runs that change nothing (test=True arg)."""
    return fun in ("state.apply", "state.highstate") and any(
        a.lower() == "test=true" for a in args)

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


OPERATION_GROUPS = [
    ("First-class", [
        {"preset": "ping", "fun": "test.ping", "args": "",
         "about": "Check minions respond"},
        {"preset": "apply", "fun": "state.apply", "args": "",
         "about": "Apply states (args: sls names)"},
        {"preset": "highstate", "fun": "state.highstate", "args": "",
         "about": "Enforce the full state tree"},
        {"preset": "highstate-dry", "fun": "state.highstate",
         "args": "test=True", "about": "Preview highstate, change nothing"},
    ]),
    ("Fleet", [
        {"preset": "pkg-install", "fun": "pkg.install", "args": "",
         "about": "Install packages (args: names)"},
        {"preset": "pkg-remove", "fun": "pkg.remove", "args": "",
         "about": "Remove packages (args: names)"},
        {"preset": "service-restart", "fun": "service.restart", "args": "",
         "about": "Restart a service (args: name)"},
        {"preset": "service-status", "fun": "service.status", "args": "",
         "about": "Check a service status (args: name)"},
        {"preset": "process-signal", "fun": "ps.kill_pid",
         "args": "<pid> <signal>", "about": "Signal a process by PID"},
        {"preset": "minion-restart", "fun": "service.restart",
         "args": "salt-minion", "about": "Restart the salt-minion service"},
        {"preset": "mine-update", "fun": "mine.update", "args": "",
         "about": "Refresh mine data"},
    ]),
    ("Pillar & sync", [
        {"preset": "refresh-pillar", "fun": "saltutil.refresh_pillar",
         "args": "", "about": "Refresh pillar data"},
        {"preset": "sync-all", "fun": "saltutil.sync_all", "args": "",
         "about": "Sync modules to minions"},
    ]),
]

_FUN_ABOUT: dict[str, str] = {}
for _group, _ops in OPERATION_GROUPS:
    for _op in _ops:
        _FUN_ABOUT.setdefault(_op["fun"], _op["about"])
OP_FUNCTIONS = [{"fun": fun, "about": about}
                for fun, about in _FUN_ABOUT.items()]


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


def live_returns_now(client, jid: str) -> list:
    """Display-only live returns from the master job cache.

    Never raises; [] when the cache expired the JID. Rows are
    lightweight stand-ins marked live=True — never DB rows, so
    the merge below can't duplicate or rewrite history.
    """
    from types import SimpleNamespace

    try:
        payload = client.runner("jobs.lookup_jid", jid=jid)[0]
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        return []
    data = payload.get(jid, payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return []
    rows = []
    for mid, ret in data.items():
        # Verified live: values are the raw returns ({minion: True}
        # for test.ping) or per-minion dicts for richer jobs.
        if isinstance(ret, dict):
            success = bool(ret.get("success", True))
            rows.append(SimpleNamespace(
                minion_id=mid, success=success,
                retcode=ret.get("retcode", 0),
                payload=ret.get("return", ret), live=True))
        else:
            rows.append(SimpleNamespace(
                minion_id=mid, success=bool(ret), retcode=0,
                payload=ret, live=True))
    return rows


def build_sls_preview(fun: str, args: list[str], matched: list | None,
                      via: str) -> tuple[list, str | None, str | None]:
    """(sections, minion, note) for the review page.

    Sections are [(sls, {state-id: ...})]; note explains a missing
    or failed render. Only state.apply with sls args renders;
    anything else returns ([], None, None) and the page is
    unchanged. Firing is never blocked: failures become notes.
    """
    if fun != "state.apply" or not args:
        return [], None, None
    from .dashboard import ping_target

    minion = (matched or [None])[0] or ping_target()
    if minion is None:
        return [], None, "No minion available to render against."
    from .tasks import queue_or_none, show_sls_now, show_sls_task, wait_for

    try:
        queued = queue_or_none(show_sls_task, minion, args, via)
        if queued is None:
            rendered = show_sls_now(get_salt(), minion, args, via)
        else:
            status, value = wait_for(queued, wait=8.0)
            if status == "pending":
                return [], minion, ("Preview unavailable: render still "
                                    "running — firing stays available.")
            if status != "ready":
                return [], minion, ("Preview unavailable: render failed "
                                    f"in the background: {value}")
            rendered = value
    except (SaltApiError, httpx.HTTPError) as exc:
        return [], minion, f"Preview unavailable: salt-api error: {exc}"
    sections = [(sls, rendered[sls]) for sls in args if sls in rendered]
    missing = [sls for sls in args if sls not in rendered]
    note = (f"Preview unavailable for: {', '.join(missing)}."
            if missing else None)
    return sections, minion, note


def resolve_group_target(name: str) -> tuple[list[str], int]:
    """Group members pinned to the snapshot roster.

    Returns (targets, stale_count). Raises SaltApiError when the
    group is unknown or nothing in it is known.
    """
    from .models import Minion, MinionGroup

    session = get_session()
    group = session.query(MinionGroup).filter_by(name=name).first()
    if group is None:
        raise SaltApiError(f"unknown group '{name}'")
    roster = {row.id for row in session.query(Minion.id).all()}
    targets = sorted(m for m in (group.members or []) if m in roster)
    if not targets:
        raise SaltApiError(f"group '{name}' matches no known minions")
    return targets, len(group.members or []) - len(targets)


def launch(tgt: str, tgt_type: str, fun: str, args: list,
           asynchronous: bool, via: str = "local") -> str:
    """Fire a job via salt-api; record the Job row. Returns the jid."""
    client = get_salt()
    if tgt_type == "group":
        targets, stale = resolve_group_target(tgt)
        if stale:
            flash(f"Group '{tgt}': {stale} stale members skipped.", "warning")
        tgt, tgt_type = ",".join(targets), "list"
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
    if not saved and not preset and not raw_bulk:
        pre_tgt = request.args.get("tgt", "").strip()
        pre_type = request.args.get("tgt_type", "")
        if pre_tgt and pre_type in TGT_TYPES:
            preset = {"tgt": pre_tgt, "tgt_type": pre_type}
            pre_fun = request.args.get("fun", "").strip()
            if pre_fun:
                preset["fun"] = pre_fun
            pre_args = request.args.get("args", "").strip()
            if pre_args:
                preset["args"] = pre_args
    if not saved and "tgt" not in preset:
        from .settings import get_setting

        preset["tgt"] = get_setting("default_target")
    op_functions = OP_FUNCTIONS
    doc_minion = ping_target()
    if doc_minion:
        from .tasks import fun_index_task, list_functions_now, queue_or_none, wait_for

        try:
            queued = queue_or_none(fun_index_task, doc_minion)
            if queued is None:
                live = list_functions_now(get_salt(), doc_minion)
            else:
                status, value = wait_for(queued, wait=6.0)
                live = value if status == "ready" else None
        except (SaltApiError, httpx.HTTPError):
            live = None
        if live:
            known = {f["fun"] for f in OP_FUNCTIONS}
            op_functions = OP_FUNCTIONS + [
                {"fun": name, "about": ""}
                for name in live if name not in known
            ]
    return render_template("job_new.html", tgt_types=TGT_TYPES,
                           preset=preset, saved=saved, bulk=bulk,
                           bulk_ignored=bulk_ignored,
                           op_groups=OPERATION_GROUPS,
                           op_functions=op_functions,
                           doc_minion=doc_minion)


@bp.get("/fun-doc")
@login_required
def fun_doc():
    """sys.doc fragment for one function, read from one minion."""
    fun = request.args.get("fun", "").strip()
    minion = request.args.get("minion", "").strip()
    if not FUN_RE.match(fun) or not minion:
        return "Invalid function name.", 400
    from .tasks import fun_doc_now

    try:
        doc = fun_doc_now(get_salt(), minion, fun)
    except (SaltApiError, httpx.HTTPError):
        doc = ""
    return render_template("_fun_doc.html", fun=fun, doc=doc)


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
        flash("Target type and function are required.", "error")
        return redirect(url_for("jobs.new"))
    if via == "ssh" and asynchronous:
        asynchronous = False
        flash("salt-ssh runs synchronously; switched to sync mode.", "info")
    if (fun in CONFIRM_FUNS and not is_test_mode(fun, args)
            and request.form.get("confirmed", "") != "yes"):
        batch_preview = parse_batch_fields(request.form) or {}
        matched = resolve_batch_roster(tgt, tgt_type)
        preview, preview_minion, preview_note = build_sls_preview(
            fun, args, matched, via)
        return render_template("job_confirm.html", tgt=tgt, tgt_type=tgt_type,
                               fun=fun, raw_args=raw_args,
                               mode=request.form.get("mode", "async"),
                               via=via,
                               save_as=request.form.get("save_as", ""),
                               batch_mode=batch_preview.get("mode", "off"),
                               batch_size=batch_preview.get("size", 25),
                               stop_after=batch_preview.get("stop_after", 1),
                               matched=matched, preview=preview,
                               preview_minion=preview_minion,
                               preview_note=preview_note)
    batch = parse_batch_fields(request.form)
    if batch is not None:
        return run_batched(tgt, tgt_type, fun, args, batch,
                           save_as=request.form.get("save_as", ""))
    try:
        jid = launch(tgt, tgt_type, fun, args, asynchronous, via=via)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("jobs.new"))
    if request.form.get("save_as"):
        session = get_session()
        session.add(SavedJob(name=request.form["save_as"], fun=fun, tgt=tgt,
                             tgt_type=tgt_type, args=args))
        session.commit()
    return redirect(url_for("jobs.detail", jid=jid))


def parse_batch_fields(form) -> dict | None:
    """Batch config from the run form, or None for a normal run."""
    mode = form.get("batch_mode", "off")
    if mode not in ("count", "percent"):
        return None
    try:
        size = int(form.get("batch_size", "0"))
        stop_after = int(form.get("stop_after", "1"))
    except ValueError:
        return None
    if size < 1 or stop_after < 1:
        return None
    return {"mode": mode, "size": size, "stop_after": stop_after}


def resolve_batch_roster(tgt: str, tgt_type: str) -> list[str] | None:
    """Pin the wave roster from the snapshot table. List and glob only."""
    import fnmatch

    from .models import Minion

    roster = sorted(row.id for row in get_session().query(Minion.id).all())
    if tgt_type == "list":
        wanted = [t.strip() for t in tgt.split(",") if t.strip()]
        return [mid for mid in roster if mid in wanted]
    if tgt_type == "glob":
        return [mid for mid in roster if fnmatch.fnmatchcase(mid, tgt)]
    if tgt_type == "group":
        try:
            targets, _ = resolve_group_target(tgt)
        except SaltApiError:
            return []
        return targets
    return None


def run_batched(tgt: str, tgt_type: str, fun: str, args: list,
                batch: dict, save_as: str = ""):
    """Start a gated wave batch: parent row, enqueue or run inline."""
    import uuid

    from .audit import log_event
    from .models import Job
    from .tasks import queue_or_none, run_wave_batch, run_wave_batch_task

    roster = resolve_batch_roster(tgt, tgt_type)
    if roster is None:
        flash("Batch mode supports list, glob, and group targets.", "error")
        return redirect(url_for("jobs.new"))
    if not roster:
        flash("Batch roster is empty: no known minions match.", "error")
        return redirect(url_for("jobs.new"))
    from .tasks import split_roster

    waves = split_roster(roster, batch["mode"], batch["size"])
    group = uuid.uuid4().hex[:16]
    session = get_session()
    session.add(Job(
        jid=f"batch-{group}", fun=fun, tgt=tgt, tgt_type=tgt_type,
        user=current_user.username, batch_group=group,
        batch_state={"mode": batch["mode"], "size": batch["size"],
                     "stop_after": batch["stop_after"], "status": "running",
                     "failures": 0, "waves": len(waves),
                     "waves_done": 0}))
    session.commit()
    log_event(current_user.username, f"batch-start:{group}")
    if save_as:
        session.add(SavedJob(name=save_as, fun=fun, tgt=tgt,
                             tgt_type=tgt_type, args=args, batch=batch))
        session.commit()
    job = queue_or_none(run_wave_batch_task, group, waves, fun, args,
                        batch["stop_after"], current_user.username)
    if job is None:
        result = run_wave_batch(group, waves, fun, args,
                                batch["stop_after"], current_user.username)
        flash(f"Batch {result['status']}: "
              f"{result['failures']} failures over {len(waves)} waves.", "warning")
    else:
        flash(f"Batch queued: {len(waves)} waves. Watch this page.", "success")
    return redirect(url_for("jobs.detail", jid=f"batch-{group}"))


MODS_RE = __import__("re").compile(r"^[A-Za-z0-9_.-]+$")


@bp.route("/orchestrate")
@login_required
def orchestrate():
    return render_template("jobs_orchestrate.html")


@bp.post("/orchestrate/run")
@roles_required("operator")
def orchestrate_run():
    import json as _json
    import time as _time

    from .audit import log_event
    from .tasks import queue_or_none, run_orchestrate_task

    mods = request.form.get("mods", "").strip()
    saltenv = request.form.get("saltenv", "").strip() or "base"
    test = request.form.get("test", "") == "on"
    raw_pillar = request.form.get("pillar", "").strip()
    pillar: dict = {}
    if raw_pillar:
        try:
            pillar = _json.loads(raw_pillar)
        except ValueError:
            flash("Pillar override is not valid JSON.", "error")
            return redirect(url_for("jobs.orchestrate"))
        if not isinstance(pillar, dict):
            flash("Pillar override must be a JSON object.", "error")
            return redirect(url_for("jobs.orchestrate"))
    if not mods or not MODS_RE.match(mods):
        flash("Mods must be a dotted orchestration name.", "error")
        return redirect(url_for("jobs.orchestrate"))
    jid = f"orch-{int(_time.time())}"
    session = get_session()
    session.add(Job(jid=jid, fun="state.orchestrate", tgt=mods,
                    tgt_type="runner", user=current_user.username))
    session.commit()
    log_event(current_user.username, f"run-orchestrate:{mods}", jid=jid)
    job = queue_or_none(run_orchestrate_task, jid, mods, saltenv, test,
                        pillar, current_user.username, job_timeout=1800)
    if job is None:
        from .tasks import run_orchestrate_task as run_inline

        run_inline(jid, mods, saltenv, test, pillar,
                   current_user.username)
        flash("Orchestration finished synchronously.", "success")
    else:
        flash("Orchestration queued. Watch this page.", "success")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/batch/<group>/cancel")
@roles_required("operator")
def cancel_batch(group: str):
    from .tasks import request_batch_cancel

    if request_batch_cancel(group):
        flash("Cancel requested: no new waves will start.", "info")
    else:
        flash("Cancel flag not stored (no queue); "
              "a running inline batch cannot stop.", "warning")
    return redirect(url_for("jobs.detail", jid=f"batch-{group}"))


@bp.route("/<jid>")
@login_required
def detail(jid: str):
    session = get_session()
    job = session.get(Job, jid)
    if job is None:
        flash("Unknown job.", "error")
        return redirect(url_for("jobs.index", tab="history"))
    waves: list = []
    batch_state: dict | None = None
    if job.batch_group and jid == f"batch-{job.batch_group}":
        waves = (session.query(Job)
                 .filter(Job.batch_group == job.batch_group,
                         Job.jid != job.jid)
                 .order_by(Job.started_at).all())
        for child in waves:
            sync_job(child.jid)
        batch_state = job.batch_state or {}
    else:
        sync_job(jid)
    returns = session.query(JobReturn).filter_by(jid=jid).all()
    if not job.complete:
        # Live master-cache rows for minions the returner hasn't
        # recorded yet (DB rows win, so no duplicates). The sync
        # button redirects here, so it shares this merge.
        seen = {r.minion_id for r in returns}
        returns = returns + [
            r for r in live_returns_now(get_salt(), jid)
            if r.minion_id not in seen
        ]
    kill_reports: list = []
    if not (job.batch_group and jid == f"batch-{job.batch_group}"):
        from .models import AuditEvent

        for event in (session.query(AuditEvent)
                      .filter_by(action=f"kill:{jid}")
                      .order_by(AuditEvent.id).all()):
            if event.jid:
                sync_job(event.jid)
                kill_reports.append(
                    (event.jid,
                     session.query(JobReturn).filter_by(jid=event.jid).all()))
    return render_template("job_detail.html", job=job, returns=returns,
                           waves=waves, batch_state=batch_state,
                           killable=killable(job),
                           kill_reports=kill_reports)


def killable(job) -> bool:
    """A job kill needs a real Salt JID and a minion target."""
    return (not job.complete
            and job.tgt_type in ("glob", "list", "grain", "compound",
                                 "nodegroup", "group")
            and not job.jid.startswith(("batch-", "ssh-", "sync-")))


@bp.post("/<jid>/kill")
@roles_required("operator")
def kill(jid: str):
    """Publish saltutil.kill_job for jid to the job's own target."""
    session = get_session()
    job = session.get(Job, jid)
    if job is None:
        flash("Unknown job.", "error")
        return redirect(url_for("jobs.index", tab="history"))
    if not killable(job):
        flash("Only running Salt jobs with minion targets can be killed.", "error")
        return redirect(url_for("jobs.detail", jid=jid))
    tgt, tgt_type = job.tgt, job.tgt_type
    if tgt_type == "group":
        try:
            targets, _ = resolve_group_target(tgt)
        except SaltApiError as exc:
            flash(f"salt-api error: {exc}", "error")
            return redirect(url_for("jobs.detail", jid=jid))
        tgt, tgt_type = ",".join(targets), "list"
    client = get_salt()
    try:
        result = client.local(tgt, "saltutil.kill_job", arg=[jid],
                              tgt_type=tgt_type, asynchronous=True)
        kill_jid = (result[0]["jid"] if isinstance(result, list)
                    else result["jid"])
    except (SaltApiError, KeyError, IndexError, TypeError) as exc:
        flash(f"kill failed: {exc}", "error")
        return redirect(url_for("jobs.detail", jid=jid))
    session.add(Job(jid=kill_jid, fun="saltutil.kill_job", tgt=tgt,
                    tgt_type=tgt_type, user=current_user.username))
    session.commit()
    log_event(current_user.username, f"kill:{jid}", jid=kill_jid)
    flash(f"Kill published for {jid}: minions report back here.", "success")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/<jid>/sync")
@roles_required("operator")
def sync(jid: str):
    try:
        sync_job(jid)
    except (SaltApiError, ValueError) as exc:
        flash(f"sync error: {exc}", "error")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/saved/<int:saved_id>/delete")
@roles_required("operator")
def delete_saved(saved_id: int):
    session = get_session()
    saved = session.get(SavedJob, saved_id)
    if saved:
        session.delete(saved)
        session.commit()
        flash(f"Deleted saved job '{saved.name}'.", "success")
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

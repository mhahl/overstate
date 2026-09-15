"""Job runner, history synced from the returner tables, saved jobs, SSE stream.

Routes only: constants and pure helpers live in
:mod:`overstate_ui.jobs_helpers`, Salt/DB service functions in
:mod:`overstate_ui.jobs_service`. Both are re-exported here so
existing ``overstate_ui.jobs.*`` import paths keep working.
"""

from __future__ import annotations

import json
import time

import httpx
from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt, ping_target
from .db import get_session
from .jobs_helpers import (
    COMPLETE_AFTER_SECONDS,
    CONFIRM_FUNS,
    DESTRUCTIVE_FUNS,
    FLEET_PRESETS,
    FUN_RE,
    JOB_SORT_COLUMNS,
    MODS_RE,
    OP_FUNCTIONS,
    OPERATION_GROUPS,
    TGT_TYPES,
    describe_return,
    is_test_mode,
    killable,
    parse_batch_fields,
    sort_jobs,
    suggest_glob,
    summarize_state_return,
)
from .jobs_service import (
    build_sls_preview,
    launch,
    live_returns_now,
    resolve_batch_roster,
    resolve_group_target,
    run_batched,
    sync_job,
)
from .models import Job, JobReturn, SavedJob
from .salt_client import SaltApiError

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

__all__ = [
    "COMPLETE_AFTER_SECONDS",
    "CONFIRM_FUNS",
    "DESTRUCTIVE_FUNS",
    "FLEET_PRESETS",
    "FUN_RE",
    "JOB_SORT_COLUMNS",
    "MODS_RE",
    "OPERATION_GROUPS",
    "OP_FUNCTIONS",
    "TGT_TYPES",
    "bp",
    "build_sls_preview",
    "is_test_mode",
    "killable",
    "launch",
    "live_returns_now",
    "parse_batch_fields",
    "resolve_batch_roster",
    "resolve_group_target",
    "run_batched",
    "sort_jobs",
    "suggest_glob",
    "sync_job",
]


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "running")
    if tab not in ("running", "history", "saved"):
        tab = "running"
    session = get_session()
    # Opportunistic sync so finished jobs land in history without opening
    # every detail page. Bounded to the 10 most recent running jobs.
    for job in (
        session.query(Job)
        .filter_by(complete=False)
        .order_by(Job.started_at.desc())
        .limit(10)
        .all()
    ):
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
    q = request.args.get("q", "").strip()
    ql = q.lower()

    def matches(job) -> bool:
        if not ql:
            return True
        return any(
            ql in (str(getattr(job, f, "") or "").lower())
            for f in ("jid", "fun", "tgt", "tgt_type", "user")
        )

    running = sort_jobs(
        [
            j
            for j in session.query(Job)
            .filter_by(complete=False)
            .order_by(Job.started_at.desc())
            .all()
            if matches(j)
        ],
        sort,
        direction,
    )
    history = sort_jobs(
        [
            j
            for j in session.query(Job)
            .filter_by(complete=True)
            .order_by(Job.started_at.desc())
            .limit(200)
            .all()
            if matches(j)
        ][:50],
        sort,
        direction,
    )
    saved = session.query(SavedJob).order_by(SavedJob.name).all()
    if ql:
        saved = [
            s
            for s in saved
            if ql in s.name.lower()
            or ql in (s.fun or "").lower()
            or ql in (s.tgt or "").lower()
        ]
    ctx = {
        "tab": tab,
        "running": running,
        "history": history,
        "saved": saved,
        "sort": sort,
        "direction": direction,
        "q": q,
    }
    if request.headers.get("HX-Request") == "true":
        return render_template("_job_rows.html", **ctx)
    return render_template("jobs.html", **ctx)


@bp.route("/new")
@login_required
def new():
    preset = request.args.get("preset", "")
    presets = {
        "ping": {"tgt": "*", "tgt_type": "glob", "fun": "test.ping", "args": ""},
        "highstate": {
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.highstate",
            "args": "",
        },
        "highstate-dry": {
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.highstate",
            "args": "test=True",
        },
        "apply": {"tgt": "*", "tgt_type": "glob", "fun": "state.apply", "args": ""},
        **FLEET_PRESETS,
    }
    saved = None
    if request.args.get("saved"):
        saved = get_session().get(SavedJob, int(request.args["saved"]))
    preset = dict(presets.get(preset, {}))
    bulk = None
    raw_bulk = []
    for value in request.args.getlist("bulk"):
        raw_bulk.extend(v for v in value.split(",") if v.strip())
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
        # Echo-back after a validation failure or a confirm cancel: only
        # allow-listed values land in the form, so a crafted URL cannot
        # smuggle markup into the rendered fields.
        for key, allowed in (
            ("mode", ("async", "sync")),
            ("via", ("local", "ssh")),
            ("batch_mode", ("off", "count", "percent")),
        ):
            value = request.args.get(key, "")
            if value in allowed:
                preset[key] = value
        save_as = request.args.get("save_as", "")
        if save_as:
            preset["save_as"] = save_as
        for key in ("batch_size", "stop_after"):
            value = request.args.get(key, "")
            if value.isdigit():
                preset[key] = value
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
                {"fun": name, "about": ""} for name in live if name not in known
            ]
    return render_template(
        "job_new.html",
        tgt_types=TGT_TYPES,
        preset=preset,
        saved=saved,
        bulk=bulk,
        bulk_ignored=bulk_ignored,
        op_groups=OPERATION_GROUPS,
        op_functions=op_functions,
        doc_minion=doc_minion,
    )


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


def _new_url():
    """Back to the run form with the submitted values echoed as preset
    args, so a validation failure never wipes what was typed."""
    params = {}
    for key in (
        "tgt",
        "tgt_type",
        "fun",
        "args",
        "mode",
        "via",
        "save_as",
        "batch_mode",
        "batch_size",
        "stop_after",
    ):
        value = request.form.get(key, "")
        if value:
            params[key] = value
    return url_for("jobs.new", **params)


@bp.post("/run")
@roles_required("operator")
def run():
    tgt = request.form.get("tgt", "").strip()
    tgt_type = request.form.get("tgt_type", "glob")
    fun = request.form.get("fun", "").strip()
    raw_args = request.form.get("args", "").strip()
    args = [a for a in raw_args.split() if a] if raw_args else []
    asynchronous = request.form.get("mode", "async") == "async"
    via = request.form.get("via", "local")
    if via not in ("local", "ssh"):
        via = "local"
    if not fun or tgt_type not in TGT_TYPES:
        flash("Pick a target type and a function.", "error")
        return redirect(_new_url())
    if not tgt:
        flash("Pick a target: an empty target never fires.", "error")
        return redirect(_new_url())
    if request.form.get("batch_mode", "off") in ("count", "percent") and parse_batch_fields(
        request.form
    ) is None:
        flash("Batch wave size and stop-after must be positive numbers.", "error")
        return redirect(_new_url())
    if via == "ssh" and asynchronous:
        asynchronous = False
        flash("salt-ssh runs synchronously, in sync mode only.", "info")
    if (
        fun in CONFIRM_FUNS
        and not is_test_mode(fun, args)
        and request.form.get("confirmed", "") != "yes"
    ):
        batch_preview = parse_batch_fields(request.form) or {}
        matched = resolve_batch_roster(tgt, tgt_type)
        preview, preview_minion, preview_note = build_sls_preview(
            fun, args, matched, via
        )
        return render_template(
            "job_confirm.html",
            tgt=tgt,
            tgt_type=tgt_type,
            fun=fun,
            raw_args=raw_args,
            mode=request.form.get("mode", "async"),
            via=via,
            save_as=request.form.get("save_as", ""),
            batch_mode=batch_preview.get("mode", "off"),
            batch_size=batch_preview.get("size", 25),
            stop_after=batch_preview.get("stop_after", 1),
            matched=matched,
            preview=preview,
            preview_minion=preview_minion,
            preview_note=preview_note,
        )
    batch = parse_batch_fields(request.form)
    if batch is not None:
        return run_batched(
            tgt, tgt_type, fun, args, batch, save_as=request.form.get("save_as", "")
        )
    try:
        jid = launch(tgt, tgt_type, fun, args, asynchronous, via=via)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(_new_url())
    if request.form.get("save_as"):
        session = get_session()
        session.add(
            SavedJob(
                name=request.form["save_as"],
                fun=fun,
                tgt=tgt,
                tgt_type=tgt_type,
                args=args,
            )
        )
        session.commit()
    return redirect(url_for("jobs.detail", jid=jid))


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
    session.add(
        Job(
            jid=jid,
            fun="state.orchestrate",
            tgt=mods,
            tgt_type="runner",
            user=current_user.username,
        )
    )
    session.commit()
    log_event(current_user.username, f"run-orchestrate:{mods}", jid=jid)
    job = queue_or_none(
        run_orchestrate_task,
        jid,
        mods,
        saltenv,
        test,
        pillar,
        current_user.username,
        job_timeout=1800,
    )
    if job is None:
        from .tasks import run_orchestrate_task as run_inline

        run_inline(jid, mods, saltenv, test, pillar, current_user.username)
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
        flash(
            "Cancel flag not stored (no queue); a running inline batch cannot stop.",
            "warning",
        )
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
        waves = (
            session.query(Job)
            .filter(Job.batch_group == job.batch_group, Job.jid != job.jid)
            .order_by(Job.started_at)
            .all()
        )
        for child in waves:
            sync_job(child.jid)
        batch_state = job.batch_state or {}
        # The parent row is a grouping record Salt never ran, so it has
        # no returns of its own: aggregate the wave returns instead.
        wave_jids = [child.jid for child in waves]
        returns = (
            session.query(JobReturn)
            .filter(JobReturn.jid.in_(wave_jids))
            .order_by(JobReturn.minion_id)
            .all()
            if wave_jids
            else []
        )
    else:
        sync_job(jid)
        returns = session.query(JobReturn).filter_by(jid=jid).all()
    if not job.complete:
        # Live master-cache rows for minions the returner hasn't
        # recorded yet (DB rows win, so no duplicates). The sync
        # button redirects here, so it shares this merge.
        seen = {r.minion_id for r in returns}
        returns = returns + [
            r for r in live_returns_now(get_salt(), jid) if r.minion_id not in seen
        ]
    kill_reports: list = []
    if not (job.batch_group and jid == f"batch-{job.batch_group}"):
        from .models import AuditEvent

        for event in (
            session.query(AuditEvent)
            .filter_by(action=f"kill:{jid}")
            .order_by(AuditEvent.id)
            .all()
        ):
            if event.jid:
                sync_job(event.jid)
                kill_reports.append(
                    (event.jid, session.query(JobReturn).filter_by(jid=event.jid).all())
                )
    return render_template(
        "job_detail.html",
        job=job,
        returns=returns,
        waves=waves,
        batch_state=batch_state,
        killable=killable(job),
        kill_reports=kill_reports,
        state_summary=summarize_state_return,
        describe_return=describe_return,
    )


@bp.get("/<jid>/panel/<mid>")
@login_required
def panel(jid: str, mid: str):
    """One minion's return panel fragment for live list updates.

    Prefers the stored returner row; falls back to the live master
    cache while the job runs. 404 when the minion has neither, so the
    page simply skips minions with nothing to show yet.
    """
    session = get_session()
    job = session.get(Job, jid)
    if job is None:
        abort(404)
    row = None
    if job.batch_group and jid == f"batch-{job.batch_group}":
        wave_jids = [
            row_jid
            for (row_jid,) in session.query(Job.jid)
            .filter(Job.batch_group == job.batch_group, Job.jid != jid)
            .all()
        ]
        if wave_jids:
            row = (
                session.query(JobReturn)
                .filter(JobReturn.jid.in_(wave_jids), JobReturn.minion_id == mid)
                .order_by(JobReturn.jid.desc())
                .first()
            )
    else:
        sync_job(jid)
        row = session.query(JobReturn).filter_by(jid=jid, minion_id=mid).first()
    if row is None and not job.complete:
        live = [r for r in live_returns_now(get_salt(), jid) if r.minion_id == mid]
        row = live[0] if live else None
    if row is None:
        abort(404)
    return render_template(
        "_job_panel.html", r=row, job=job, view=describe_return(row.payload)
    )


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
        result = client.local(
            tgt, "saltutil.kill_job", arg=[jid], tgt_type=tgt_type, asynchronous=True
        )
        kill_jid = result[0]["jid"] if isinstance(result, list) else result["jid"]
    except (SaltApiError, KeyError, IndexError, TypeError) as exc:
        flash(f"kill failed: {exc}", "error")
        return redirect(url_for("jobs.detail", jid=jid))
    session.add(
        Job(
            jid=kill_jid,
            fun="saltutil.kill_job",
            tgt=tgt,
            tgt_type=tgt_type,
            user=current_user.username,
        )
    )
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
    if saved is None:
        flash("No such saved job.", "error")
    else:
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
            jids = [jid]
            if (
                job is not None
                and job.batch_group
                and jid == f"batch-{job.batch_group}"
            ):
                rows = (
                    get_session()
                    .query(Job.jid)
                    .filter(Job.batch_group == job.batch_group, Job.jid != jid)
                    .all()
                )
                jids = [row[0] for row in rows] or [jid]
            returns = (
                get_session().query(JobReturn).filter(JobReturn.jid.in_(jids)).all()
            )
            mids = {r.minion_id for r in returns}
            is_parent = bool(
                job and job.batch_group and jid == f"batch-{job.batch_group}"
            )
            if job is not None and not job.complete and not is_parent:
                # Live-cache minions the returner hasn't recorded yet so
                # the page can render their panels before the rows land.
                mids |= {r.minion_id for r in live_returns_now(get_salt(), jid)}
            payload = {
                "jid": jid,
                "complete": bool(job and job.complete),
                "returned": len(returns),
                "failed": sum(1 for r in returns if not r.success),
                "minions": sorted(mids),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if payload["complete"]:
                break
            time.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream")

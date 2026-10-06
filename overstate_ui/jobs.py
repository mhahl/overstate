"""Job runner, history synced from the returner tables, saved jobs, SSE stream.

Routes only: constants and pure helpers live in
:mod:`overstate_ui.jobs_helpers`, Salt/DB service functions in
:mod:`overstate_ui.jobs_service`. Both are re-exported here so
existing ``overstate_ui.jobs.*`` import paths keep working.
"""

from __future__ import annotations

import json
import logging
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
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from .audit import log_event
from .auth import roles_required
from .authz import (
    AuthzDenied,
    audit_deny,
    authorize,
    can_see_return_body,
    constrain_target_verbose,
    has_fleet,
    minions_with,
    publish_perm,
    rbac_mode,
    require,
)
from .dashboard import get_salt, ping_target
from .db import get_session
from .jobs_helpers import (
    ALLOWED_FUNS,
    COMPLETE_AFTER_SECONDS,
    CONFIRM_FUNS,
    DESTRUCTIVE_FUNS,
    FLEET_PRESETS,
    FUN_RE,
    JOB_SORT_COLUMNS,
    MODS_RE,
    OP_FUNCTIONS,
    OPERATION_GROUPS,
    SALTENV_RE,
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

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_FUNS",
    "COMPLETE_AFTER_SECONDS",
    "CONFIRM_FUNS",
    "DESTRUCTIVE_FUNS",
    "FLEET_PRESETS",
    "FUN_RE",
    "JOB_SORT_COLUMNS",
    "MODS_RE",
    "OPERATION_GROUPS",
    "OP_FUNCTIONS",
    "SALTENV_RE",
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


def _published_ids(tgt: str | None, tgt_type: str | None) -> set[str] | None:
    """Snapshot ids a stored target names, or None when the type cannot
    be evaluated locally (compound, nodegroup, runner, unknown)."""
    from .authz import published_snapshot_ids

    return published_snapshot_ids(tgt, tgt_type)


def _job_allowed_ids(job) -> set[str] | None:
    """Published ids of ``job`` the caller may see, or None for fleet
    ``job.read`` (the stored target renders unchanged)."""
    if has_fleet(current_user, "job.read"):
        return None
    ids = _published_ids(job.tgt, job.tgt_type)
    if ids is None:
        return set()
    return ids & minions_with(current_user, "job.read")


def _display_tgt(tgt: str, allowed: set[str] | None) -> str:
    if allowed is None:
        return tgt
    return ",".join(sorted(allowed))


def _redacted_rows(rows, fun_of) -> list:
    """Filter returns to the caller's job.read scope and blank bodies
    the return-visibility table withholds. ORM rows are never mutated:
    the session commits on teardown and would persist a blanking."""
    from types import SimpleNamespace

    from .authz import can_see_return_body

    if rbac_mode() != "scoped":
        return list(rows)
    fleet = has_fleet(current_user, "job.read")
    allowed = None if fleet else minions_with(current_user, "job.read")
    out = []
    for row in rows:
        mid = row.minion_id
        if allowed is not None and mid not in allowed:
            continue
        payload = row.payload
        if not can_see_return_body(current_user, fun_of(row), mid):
            payload = {}
        out.append(
            SimpleNamespace(
                minion_id=mid,
                success=row.success,
                retcode=row.retcode,
                payload=payload,
                live=getattr(row, "live", False),
            )
        )
    return out


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "running")
    if tab not in ("running", "history", "saved"):
        tab = "running"
    session = get_session()
    scoped = rbac_mode() == "scoped"
    if scoped:
        require("job.read")
    fleet_read = not scoped or has_fleet(current_user, "job.read")
    allowed_read = None if fleet_read else minions_with(current_user, "job.read")

    def _visible(job) -> bool:
        if fleet_read:
            return True
        ids = _published_ids(job.tgt, job.tgt_type)
        return bool(ids and ids & allowed_read)

    # Opportunistic sync so finished jobs land in history without opening
    # every detail page. Bounded to the 10 most recent running jobs.
    for job in (
        session.query(Job)
        .filter_by(complete=False)
        .order_by(Job.started_at.desc())
        .limit(10)
        .all()
    ):
        if scoped and not _visible(job):
            continue
        try:
            sync_job(job.jid)
        except (SaltApiError, ValueError) as exc:
            # Opportunistic sync must never hide a failure from the logs.
            logger.warning("sync_job(%s) failed: %s", job.jid, exc)
    sort = request.args.get("sort", "started")
    if sort not in JOB_SORT_COLUMNS:
        sort = "started"
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    q = request.args.get("q", "").strip()
    ql = q.lower()
    jump = request.args.get("jump", "").strip()
    if jump:
        # Exact JID lookup from the history search box: old jobs live
        # beyond any page window, so jump straight to the detail page.
        if session.get(Job, jump) is not None:
            return redirect(url_for("jobs.detail", jid=jump))
        flash("Unknown job.", "error")
        return redirect(
            url_for("jobs.index", tab="history", q=q, sort=sort, dir=direction)
        )
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50

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
            if matches(j) and (not scoped or _visible(j))
        ],
        sort,
        direction,
    )
    # History search hits the database so a pasted old JID finds its
    # row instead of only scanning the newest page in Python.
    hist_query = session.query(Job).filter_by(complete=True)
    if q:
        like = f"%{q}%"
        hist_query = hist_query.filter(
            or_(
                Job.jid.ilike(like),
                Job.fun.ilike(like),
                Job.tgt.ilike(like),
                Job.user.ilike(like),
            )
        )
    total = hist_query.count()
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    history = sort_jobs(
        [
            j
            for j in hist_query.order_by(Job.started_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
            if not scoped or _visible(j)
        ],
        sort,
        direction,
    )
    saved = session.query(SavedJob).order_by(SavedJob.name).all()
    if scoped and not fleet_read:
        # Same subset rule as delete_saved: overlap is not enough, and
        # unevaluable targets stay hidden. Stale targets (no snapshot
        # ids left) still list, matching the deletable rule.
        listed = []
        for s in saved:
            mids = _published_ids(s.tgt, s.tgt_type)
            if mids is not None and mids <= allowed_read:
                listed.append(s)
        saved = listed
    if ql:
        saved = [
            s
            for s in saved
            if ql in s.name.lower()
            or ql in (s.fun or "").lower()
            or ql in (s.tgt or "").lower()
        ]
    tgt_display = {}
    if scoped and not fleet_read:
        for j in list(running) + list(history):
            ids = _published_ids(j.tgt, j.tgt_type) or set()
            tgt_display[j.jid] = _display_tgt(j.tgt, ids & allowed_read)
        for s in saved:
            ids = _published_ids(s.tgt, s.tgt_type) or set()
            tgt_display[f"saved:{s.id}"] = _display_tgt(s.tgt, ids & allowed_read)
    ctx = {
        "tab": tab,
        "running": running,
        "history": history,
        "saved": saved,
        "sort": sort,
        "direction": direction,
        "q": q,
        "page": page,
        "pages": pages,
        "tgt_display": tgt_display,
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
    from .authz import has_fleet as _has_fleet
    from .authz import rbac_mode as _rbac_mode
    from .authz import require as _require
    from .authz import runnable_ids as _runnable_ids
    from .authz import single_group_scope as _single_group_scope

    _scoped = _rbac_mode() == "scoped"
    if _scoped:
        _require("job.read")
    _fleet_run = not _scoped or _has_fleet(
        current_user, "job.run.read"
    ) or _has_fleet(current_user, "job.run.change") or _has_fleet(
        current_user, "job.run.state"
    )
    saved = None
    if request.args.get("saved"):
        try:
            saved = get_session().get(SavedJob, int(request.args["saved"]))
        except ValueError:
            # Garbage query args land back on a clean form, never a 500.
            flash("Invalid saved job reference.", "error")
            saved = None
        if (
            _scoped
            and saved is not None
            and not _fleet_run
            and not _job_allowed_ids(saved)
        ):
            flash("Saved job is outside your scope.", "error")
            saved = None
    if request.args.get("bulk_run") and not request.args.getlist("bulk"):
        # The minion list submits here with no selection: say so instead
        # of rendering a blank form as if nothing happened.
        flash("Select minions first.", "warning")
    preset = dict(presets.get(preset, {}))
    bulk = None
    raw_bulk = []
    for value in request.args.getlist("bulk"):
        raw_bulk.extend(v for v in value.split(",") if v.strip())
    if raw_bulk and not saved and not preset:
        from .models import Minion

        roster = [row.id for row in get_session().query(Minion.id).all()]
        if _scoped and not _fleet_run:
            # "Select all" on a filtered page must not compute a fleet *:
            # suggest_glob sees only the caller's runnable ids.
            runnable = _runnable_ids(current_user)
            roster = [m for m in roster if m in runnable]
            raw_bulk = [m for m in raw_bulk if m in runnable]
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
        if _scoped and not _fleet_run:
            # Never prefill * for a caller without fleet run scope: one
            # group scope prefills as a group target, else empty.
            group_name = _single_group_scope(
                current_user, ("job.run.read", "job.run.change", "job.run.state")
            )
            if group_name is not None:
                preset["tgt"] = group_name
                preset["tgt_type"] = "group"
            else:
                preset["tgt"] = ""
                preset["tgt_type"] = "list"
        else:
            from .settings import get_setting

            preset["tgt"] = get_setting("default_target")
    op_functions = OP_FUNCTIONS
    doc_minion = ping_target()
    # Do not probe list_functions on an out-of-scope reader.
    if (
        _scoped
        and doc_minion is not None
        and not _fleet_run
        and doc_minion not in _runnable_ids(current_user)
    ):
        doc_minion = None
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
    from .authz import rbac_mode as _mode
    from .authz import require as _req

    if _mode() == "scoped":
        _req("job.read", minion=minion)
    if not FUN_RE.match(fun) or not minion or any(c in minion for c in "*?[]"):
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
    if (
        request.form.get("batch_mode", "off") in ("count", "percent")
        and parse_batch_fields(request.form) is None
    ):
        flash("Batch wave size and stop-after must be positive numbers.", "error")
        return redirect(_new_url())
    if via == "ssh" and asynchronous:
        asynchronous = False
        flash("salt-ssh runs in sync mode only.", "info")
    if not FUN_RE.match(fun) or fun not in ALLOWED_FUNS:
        flash("That function cannot run here.", "error")
        return redirect(_new_url())
    if fun == "schedule.add":
        for arg in args:
            name, sep, value = arg.partition("=")
            if (
                sep
                and name == "function"
                and (not FUN_RE.match(value) or value not in ALLOWED_FUNS)
            ):
                flash("That function cannot run here.", "error")
                return redirect(_new_url())
    scoped = rbac_mode() == "scoped"
    perm = publish_perm(fun) or "" if scoped else ""
    if scoped:
        # Backstop before the confirm page: a caller with no grant for
        # this function class never sees the matched-minion preview.
        require(perm)
    if fun in CONFIRM_FUNS and not is_test_mode(fun, args):
        batch_preview = parse_batch_fields(request.form) or {}
        matched = resolve_batch_roster(tgt, tgt_type)
        if scoped:
            try:
                _, _, requested, hit = constrain_target_verbose(
                    current_user, perm, tgt, tgt_type
                )
            except AuthzDenied as exc:
                # Do not render the confirm template: it would list
                # matched ids the caller must not see.
                audit_deny(exc.perm, detail=exc.detail)
                abort(403)
            if hit is None:
                # Fleet grant: the target publishes unchanged, so there
                # is no narrowing to flash; the preview renders the
                # snapshot roster, as the legacy path does.
                dropped = set()
            else:
                matched = sorted(hit)
                dropped = requested - hit
            if dropped:
                flash(
                    f"{len(dropped)} minions are outside your scope and "
                    "will not be touched.",
                    "warning",
                )
            preview_ids = list(hit) if hit is not None else list(matched or [])
            if not preview_ids:
                # Unevaluable fleet target (compound, nodegroup): the
                # preview renders against the snapshot head, so that id
                # is what the pillar check covers.
                head = ping_target()
                if head is not None:
                    preview_ids = [head]
            if (
                fun == "state.apply"
                and args
                and preview_ids
                and not all(
                    authorize(current_user, "pillar.read", minion=m)
                    for m in preview_ids
                )
            ):
                preview, preview_minion, preview_note = (
                    [],
                    None,
                    "SLS render hidden: outside your pillar scope.",
                )
            else:
                preview, preview_minion, preview_note = build_sls_preview(
                    fun, args, matched, via
                )
        else:
            preview, preview_minion, preview_note = build_sls_preview(
                fun, args, matched, via
            )
        confirmed = request.form.get("confirmed", "") == "yes"
        typed_ok = request.form.get("confirm_tgt", "") == tgt
        preview_ok = matched is not None or request.form.get("no_preview_ok") == "on"
        if not (confirmed and typed_ok and preview_ok):
            if confirmed and not typed_ok:
                flash("Type the target exactly to confirm.", "error")
            elif confirmed:
                flash("Confirm firing without a match preview.", "error")
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
        # run_batched owns the scoped constrain (unknown group and empty
        # intersection are 403 there) plus the job.batch roster check.
        return run_batched(
            tgt, tgt_type, fun, args, batch, save_as=request.form.get("save_as", "")
        )
    if scoped:
        # Resolve the save scope before publish; launch owns the single
        # authoritative constrain (and its job-constrained audit), so the
        # original target is what Salt sees constrained.
        try:
            _, _, _, hit = constrain_target_verbose(
                current_user, perm, tgt, tgt_type
            )
        except AuthzDenied as exc:
            audit_deny(exc.perm, detail=exc.detail)
            abort(403)
        if (
            request.form.get("save_as")
            and not has_fleet(current_user, "job.save")
            and not set(hit) <= minions_with(current_user, "job.save")
        ):
            audit_deny("job.save", detail="out-of-scope")
            abort(403)
        try:
            jid = launch(tgt, tgt_type, fun, args, asynchronous, via=via)
        except AuthzDenied as exc:
            audit_deny(exc.perm, detail=exc.detail)
            abort(403)
        except SaltApiError as exc:
            flash(f"salt-api error: {exc}", "error")
            return redirect(_new_url())
    else:
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
        try:
            session.commit()
        except IntegrityError:
            # The job already ran: keep its JID, drop only the duplicate save.
            session.rollback()
            flash("Name taken. The job still ran.", "warning")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.route("/orchestrate")
@login_required
def orchestrate():
    if rbac_mode() == "scoped":
        # The runner can target anything inside the SLS; a minion
        # scope cannot contain it, so this stays fleet-only.
        require("job.run.orchestrate")
    return render_template("jobs_orchestrate.html")


@bp.post("/orchestrate/run")
@roles_required("operator")
def orchestrate_run():
    if rbac_mode() == "scoped":
        require("job.run.orchestrate")
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
    if not SALTENV_RE.match(saltenv):
        flash("Saltenv may only contain letters, numbers, dashes.", "error")
        return redirect(url_for("jobs.orchestrate"))

    def _orchestrate_confirm():
        summary = f"saltenv={saltenv}{' test=True' if test else ''}"
        if pillar:
            summary += " with pillar override"
        return render_template(
            "job_confirm.html",
            tgt=mods,
            tgt_type="runner",
            fun="state.orchestrate",
            raw_args=summary,
            mode="run",
            via="master",
            save_as="",
            batch_mode="off",
            batch_size=25,
            stop_after=1,
            matched=None,
            preview=[],
            preview_minion=None,
            preview_note=None,
            confirm_action=url_for("jobs.orchestrate_run"),
            cancel_url=url_for("jobs.orchestrate"),
            extra_hidden={
                "mods": mods,
                "saltenv": saltenv,
                "pillar": raw_pillar,
                "test": "on" if test else "",
            },
        )

    if request.form.get("confirmed", "") != "yes":
        return _orchestrate_confirm()
    if request.form.get("confirm_tgt", "") != mods:
        flash("Type the orchestration name exactly to confirm.", "error")
        return _orchestrate_confirm()
    if request.form.get("no_preview_ok") != "on":
        flash("Confirm firing without a match preview.", "error")
        return _orchestrate_confirm()
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

        outcome = run_inline(jid, mods, saltenv, test, pillar, current_user.username)
        if isinstance(outcome, dict) and outcome.get("error"):
            flash(f"Orchestration failed: {outcome['error']}", "error")
        else:
            flash("Orchestration finished synchronously.", "success")
    else:
        flash("Orchestration queued.", "success")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/batch/<group>/cancel")
@roles_required("operator")
def cancel_batch(group: str):
    from .tasks import request_batch_cancel

    if rbac_mode() == "scoped":
        # Cancel only if the caller started the batch and still holds
        # job.batch on the remaining pinned ids, or holds it on fleet.
        parent = get_session().get(Job, f"batch-{group}")
        if (
            parent is not None
            and current_user.username != parent.user
            and not has_fleet(current_user, "job.batch")
        ):
            audit_deny("job.batch", detail="out-of-scope")
            abort(403)
        if parent is not None and not has_fleet(current_user, "job.batch"):
            from .jobs_service import resolve_batch_roster

            remaining = set(resolve_batch_roster(parent.tgt, parent.tgt_type) or [])
            if not remaining or not remaining <= minions_with(
                current_user, "job.batch"
            ):
                audit_deny("job.batch", detail="out-of-scope")
                abort(403)
    if request_batch_cancel(group):
        flash("Cancel requested: no new waves will start.", "info")
    else:
        flash(
            "No queue, so the cancel flag was not stored. A running inline batch cannot stop.",
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
    if rbac_mode() == "scoped":
        require("job.read")
        if not has_fleet(current_user, "job.read") and not _job_allowed_ids(job):
            # Render only jobs whose published ids intersect job.read;
            # the rendered tgt is the published ids the caller may see.
            audit_deny("job.read", detail="out-of-scope")
            abort(403)
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
    tgt_display: dict = {}
    if rbac_mode() == "scoped" and not has_fleet(current_user, "job.read"):
        fun_of = {job.jid: job.fun}
        fun_of.update({w.jid: w.fun for w in waves})
        returns = _redacted_rows(
            returns, lambda r: fun_of.get(getattr(r, "jid", jid), job.fun)
        )
        kill_reports = [
            (kill_jid, _redacted_rows(rows, lambda r: "saltutil.kill_job"))
            for kill_jid, rows in kill_reports
        ]
        allowed = _job_allowed_ids(job)
        tgt_display[job.jid] = _display_tgt(job.tgt, allowed)
        for w in waves:
            wids = _published_ids(w.tgt, w.tgt_type) or set()
            tgt_display[w.jid] = _display_tgt(
                w.tgt, wids & allowed if allowed is not None else None
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
        tgt_display=tgt_display,
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
    if rbac_mode() == "scoped":
        # One minion's panel: the minion id is the scope check, and the
        # body follows the return-visibility table from job.fun.
        require("job.read", minion=mid)
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
    if rbac_mode() == "scoped" and not can_see_return_body(
        current_user, job.fun, mid
    ):
        from types import SimpleNamespace

        row = SimpleNamespace(
            minion_id=row.minion_id,
            success=row.success,
            retcode=row.retcode,
            payload={},
            live=getattr(row, "live", False),
        )
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
        flash("You can only kill running Salt jobs with minion targets.", "error")
        return redirect(url_for("jobs.detail", jid=jid))
    tgt, tgt_type = job.tgt, job.tgt_type
    if rbac_mode() == "scoped":
        from .authz import constrain_kill

        try:
            tgt, tgt_type = constrain_kill(current_user, tgt, tgt_type)
        except AuthzDenied as exc:
            # Never kill a subset of someone else's compound: an id
            # outside scope denies the whole kill.
            audit_deny(exc.perm, detail=exc.detail)
            abort(403)
    elif tgt_type == "group":
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
    kill_ids = [m.strip() for m in tgt.split(",") if m.strip()]
    log_event(
        current_user.username,
        f"kill:{jid}",
        jid=kill_jid,
        permission="job.kill",
        minion_id=kill_ids[0] if len(kill_ids) == 1 else None,
        detail=",".join(kill_ids) if len(kill_ids) != 1 else None,
    )
    flash(f"Kill published for {jid}. Minion reports appear on this page.", "success")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/<jid>/sync")
@roles_required("operator")
def sync(jid: str):
    if rbac_mode() == "scoped":
        # Treated as job.read so the POST matches the GET.
        require("job.read")
    try:
        sync_job(jid)
    except (SaltApiError, ValueError) as exc:
        flash(f"sync error: {exc}", "error")
    return redirect(url_for("jobs.detail", jid=jid))


@bp.post("/saved/<int:saved_id>/delete")
@roles_required("operator")
def delete_saved(saved_id: int):
    from flask import abort

    from .models import ApiToken

    session = get_session()
    saved = session.get(SavedJob, saved_id)
    if saved is None:
        flash("No such saved job.", "error")
        return redirect(url_for("jobs.index", tab="saved"))
    if rbac_mode() == "scoped":
        # Readers resolve a saved job only through publishable targets:
        # 403 for a real-but-out-of-scope id (unknown ids 404 above).
        # Stale targets (nothing left in the snapshot) stay deletable.
        require("job.save")
        if not has_fleet(current_user, "job.save"):
            mids = _published_ids(saved.tgt, saved.tgt_type)
            allowed = minions_with(current_user, "job.save")
            if mids is None or not mids <= allowed:
                audit_deny("job.save", detail="out-of-scope")
                abort(403)
    pinned = session.query(ApiToken.id).filter_by(saved_job_id=saved.id).first()
    if pinned is not None:
        # The FK is ON DELETE RESTRICT: refuse with 409 instead of a 500.
        session.rollback()
        abort(409, description="Unpin the service account token first.")
    session.delete(saved)
    session.commit()
    flash(f"Deleted saved job '{saved.name}'.", "success")
    return redirect(url_for("jobs.index", tab="saved"))


@bp.route("/<jid>/stream")
@login_required
def stream(jid: str):
    if rbac_mode() == "scoped":
        require("job.read")
        job = get_session().get(Job, jid)
        if (
            job is not None
            and not has_fleet(current_user, "job.read")
            and not _job_allowed_ids(job)
        ):
            audit_deny("job.read", detail="out-of-scope")
            abort(403)
    try:
        interval = max(0.05, min(5.0, float(request.args.get("interval", 2.0))))
    except ValueError:
        interval = 2.0

    scoped = rbac_mode() == "scoped"
    fleet = not scoped or has_fleet(current_user, "job.read")
    allowed = None if fleet else minions_with(current_user, "job.read")

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
            if allowed is not None:
                returns = [r for r in returns if r.minion_id in allowed]
            stored_ids = {r.minion_id for r in returns}
            stored_failed = sum(1 for r in returns if not r.success)
            mids = set(stored_ids)
            is_parent = bool(
                job and job.batch_group and jid == f"batch-{job.batch_group}"
            )
            live_rows: list = []
            if job is not None and not job.complete:
                # Live-cache minions the returner hasn't recorded yet so
                # the page can render their panels before the rows land.
                if is_parent:
                    for wave_jid in jids:
                        if wave_jid == jid:
                            continue
                        live_rows += live_returns_now(get_salt(), wave_jid)
                else:
                    live_rows = live_returns_now(get_salt(), jid)
                if allowed is not None:
                    live_rows = [r for r in live_rows if r.minion_id in allowed]
                mids |= {r.minion_id for r in live_rows}
            live_only = [r for r in live_rows if r.minion_id not in stored_ids]
            payload = {
                "jid": jid,
                "complete": bool(job and job.complete),
                "returned": len(mids),
                "failed": stored_failed + sum(1 for r in live_only if not r.success),
                "stored": len(returns),
                "live": len(live_only),
                "minions": sorted(mids),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if payload["complete"]:
                break
            time.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

"""Minion schedules: list, enable/disable/delete per minion. Add waits."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt
from .db import get_session
from .jobs_helpers import ALLOWED_FUNS, FUN_RE
from .minions import _exact_mid, merged_cached_roster, minion_is_up
from .models import AuditEvent
from .salt_client import SaltApiError

bp = Blueprint("schedules", __name__, url_prefix="/schedules")

SCHEDULE_ACTIONS = {
    "enable": "schedule.enable_job",
    "disable": "schedule.disable_job",
    "delete": "schedule.delete",
}

SCHEDULE_UNITS = ("seconds", "minutes", "hours", "days")


def _interval_text(job: dict) -> str:
    """Human interval for a schedule entry: 'every 60 seconds', a
    ``when:``/``cron:`` form, or a dash when neither is present."""
    if not isinstance(job, dict):
        return "—"
    for unit in SCHEDULE_UNITS:
        v = job.get(unit)
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return f"every {v} {unit}"
    if job.get("when"):
        return f"when: {job['when']}"
    if job.get("cron"):
        return f"cron: {job['cron']}"
    return "—"


def add_succeeded(result, mid: str) -> bool:
    """Normalize version-dependent ``schedule.add`` returns. Success is
    ``True`` (or a result-mapping without an explicit ``False``); anything
    falsy means Salt did not confirm the add."""
    value = result
    if isinstance(result, list) and result and isinstance(result[0], dict):
        value = result[0].get(mid, result)
    if isinstance(value, dict):
        return bool(value) and value.get("result", True) is not False
    return bool(value)


def parse_schedule_list(value) -> dict:
    """Parse schedule.list output. Some Salt versions render the schedule
    as an indented two-level string ('schedule:\\n  job:\\n    key: val').
    Returns {} when there is nothing parseable (caller shows raw output)."""
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if isinstance(v, dict)}
    if not isinstance(value, str):
        return {}
    entries: dict = {}
    current: dict | None = None
    for line in value.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 2 and line.rstrip().endswith(":"):
            current = {}
            entries[line.strip()[:-1]] = current
        elif indent >= 4 and current is not None and ":" in line:
            key, _, val = line.strip().partition(":")
            val = val.strip()
            if val.lower() in ("true", "false"):
                parsed: object = val.lower() == "true"
            else:
                try:
                    parsed = int(val)
                except ValueError:
                    parsed = val
            current[key.strip()] = parsed
    return entries


@bp.route("/")
@login_required
def index():
    from flask_login import current_user

    from .authz import has_fleet, minions_with, rbac_mode, require

    require("schedule.read")
    mid = request.args.get("minion", "")
    client = get_salt()
    # Accepted on any pod counts: a minion pinned to another pod's
    # MQ is still schedulable from here (jobs fan out to all pods).
    statuses, _, _, _ = merged_cached_roster(client)
    accepted = sorted(m for m, st in statuses.items() if st == "accepted")
    if rbac_mode() == "scoped" and not has_fleet(current_user, "schedule.read"):
        allowed = minions_with(current_user, "schedule.read")
        accepted = [m for m in accepted if m in allowed]
    if not mid and accepted:
        mid = accepted[0]
    if mid:
        # A path/query minion id is one exact minion: glob characters
        # would fan a single-minion call out to the fleet, so they 404.
        mid = _exact_mid(mid)
        if rbac_mode() == "scoped":
            require("schedule.read", minion=mid)
    entries: dict = {}
    raw = ""
    error = None
    if mid and minion_is_up(client, mid) is False:
        error = f"{mid} is not responding. Stored data is shown."
    elif mid:
        try:
            # return_yaml=False keeps this a real mapping: an empty
            # schedule arrives as {} (empty state), not blank YAML text
            # (which the raw fallback would render as a "schedule: {}" box).
            value = client.local(
                mid, "schedule.list", tgt_type="list", kwarg={"return_yaml": False}
            )[0].get(mid, {})
            entries = parse_schedule_list(value)
            if not entries and isinstance(value, str) and value.strip():
                raw = value
        except SaltApiError as exc:
            error = str(exc)
    sort = request.args.get("sort", "name")
    if sort not in ("name", "function"):
        sort = "name"
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    q = request.args.get("q", "").strip().lower()
    if isinstance(entries, dict):
        key = (
            (lambda kv: kv[0])
            if sort == "name"
            else (lambda kv: (str(kv[1].get("function", "")), kv[0]))
        )
        items = sorted(entries.items(), key=key, reverse=(direction == "desc"))
        if q:
            items = [
                (name, job)
                for name, job in items
                if q in name.lower() or q in str(job.get("function", "")).lower()
            ]
    else:
        items = []
    from .minions import resolve_page_size

    per_page = resolve_page_size(request.args.get("per_page"))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = len(items)
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    entries = dict(items[(page - 1) * per_page : page * per_page])
    return render_template(
        "schedules.html",
        minions=accepted,
        mid=mid,
        entries=entries,
        raw=raw,
        error=error,
        sort=sort,
        direction=direction,
        q=request.args.get("q", ""),
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
    )


@bp.route("/all")
@roles_required("operator")
def all_schedules():
    """Fleet-wide schedule inventory with the defining user per entry.

    Schedules authorize at define time only (see ``add``); a revoked
    grant does not stop a schedule already on a minion. This page is the
    surface to audit and revoke them. Fleet-gated: a minion-scoped
    holder cannot enumerate schedules outside their scope.
    """
    from flask import abort

    from .authz import audit_deny, has_fleet, rbac_mode, require

    if rbac_mode() == "scoped":
        require("schedule.read")
        if not has_fleet(current_user, "schedule.read"):
            audit_deny("schedule.read", detail="fleet-view-required")
            abort(403)
    client = get_salt()
    statuses, _up, _reachable, _presence_ok = merged_cached_roster(client)
    accepted = sorted(m for m, st in statuses.items() if st == "accepted")
    rows: list[dict] = []
    error = None
    if not accepted:
        error = "No accepted minions."
    else:
        try:
            payload = client.local(
                ",".join(accepted),
                "schedule.list",
                tgt_type="list",
                kwarg={"return_yaml": False},
            )[0]
        except SaltApiError as exc:
            error = str(exc)
            payload = {}
        if isinstance(payload, dict):
            session = get_session()
            owner_by: dict[tuple[str, str], str] = {}
            for ev in (
                session.query(AuditEvent)
                .filter(
                    AuditEvent.action.like("schedule-add:%"),
                    AuditEvent.minion_id.in_(accepted),
                )
                .order_by(AuditEvent.id)
                .all()
            ):
                name = ev.action[len("schedule-add:") :]
                owner_by[(ev.minion_id, name)] = ev.user
            for mid, value in payload.items():
                for name, job in parse_schedule_list(value).items():
                    rows.append(
                        {
                            "mid": mid,
                            "name": name,
                            "function": str(job.get("function", "")),
                            "when": _interval_text(job),
                            "enabled": bool(job.get("enabled", True)),
                            "owner": owner_by.get((mid, name), ""),
                        }
                    )
    q = request.args.get("q", "").strip().lower()
    if q:
        rows = [
            r
            for r in rows
            if q in r["mid"].lower() or q in r["name"].lower() or q in r["function"].lower()
        ]
    rows.sort(key=lambda r: (r["mid"], r["name"]))
    return render_template(
        "schedules_all.html",
        rows=rows,
        error=error,
        q=request.args.get("q", ""),
        total=len(rows),
    )


@bp.post("/<mid>/add")
@roles_required("operator")
def add(mid: str):
    mid = _exact_mid(mid)
    name = request.form.get("name", "").strip()
    fun = request.form.get("function", "").strip()
    mode = request.form.get("mode", "interval")
    unit = request.form.get("unit", "seconds")
    cron_expr = request.form.get("cron", "").strip()
    when_iso = request.form.get("when", "").strip()
    try:
        value = int(request.form.get("value", "").strip())
    except ValueError:
        value = 0
    enabled = request.form.get("enabled", "") == "on"
    if not name or not fun:
        flash("Name and function required.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    if not FUN_RE.match(fun) or fun not in ALLOWED_FUNS:
        flash("That function cannot run here.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    from .file_sync_schedule import parse_cron

    if mode == "cron":
        try:
            parse_cron(cron_expr)
        except ValueError:
            flash("Invalid cron expression. Use five numeric fields.", "error")
            return redirect(url_for("schedules.index", minion=mid))
        kwarg: dict = {"function": fun, "enabled": enabled, "cron": [cron_expr]}
    elif mode == "when":
        if not when_iso:
            flash("A when date is required (YYYY-MM-DDTHH:MM:SS).", "error")
            return redirect(url_for("schedules.index", minion=mid))
        kwarg = {"function": fun, "enabled": enabled, "when": [when_iso]}
    else:
        if unit not in SCHEDULE_UNITS or value < 1:
            flash("Name, function, and positive interval required.", "error")
            return redirect(url_for("schedules.index", minion=mid))
        kwarg = {"function": fun, "enabled": enabled, unit: value}
    from .authz import rbac_mode

    if rbac_mode() == "scoped":
        from flask import abort

        from .authz import audit_deny, may_define_schedule

        # Define time only: schedule.write plus the job-run class or the
        # matching schedule.allow.*. A scheduler can add state.apply and
        # still cannot POST /jobs/run for it.
        if not may_define_schedule(current_user, mid, fun):
            audit_deny("schedule.write", minion_id=mid, detail="no-grant")
            abort(403)
    client = get_salt()
    try:
        entries = parse_schedule_list(
            client.local(mid, "schedule.list", tgt_type="list")[0].get(mid, {})
        )
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("schedules.index", minion=mid))
    if name in entries:
        flash(f"{mid} already has a scheduled job named '{name}'.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    try:
        result = client.local(
            mid,
            "schedule.add",
            arg=[name],
            tgt_type="list",
            kwarg=kwarg,
        )
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        if add_succeeded(result, mid):
            log_event(current_user.username, f"schedule-add:{name}", minion_id=mid)
            flash(f"{mid}/{name}: added.", "success")
        else:
            flash(f"{mid}/{name}: salt did not confirm the add.", "warning")
    return redirect(url_for("schedules.index", minion=mid))


@bp.post("/<mid>/<action>")
@roles_required("operator")
def act(mid: str, action: str):
    from .authz import rbac_mode, require

    mid = _exact_mid(mid)
    if rbac_mode() == "scoped":
        require("schedule.write", minion=mid)
    if action not in SCHEDULE_ACTIONS:
        flash("Unknown schedule action.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    job_name = request.form.get("job", "").strip()
    if not job_name:
        flash("Select a scheduled job first. An empty selection runs nothing.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    try:
        payload = get_salt().local(
            mid, SCHEDULE_ACTIONS[action], arg=[job_name], tgt_type="list"
        )
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        data = payload[0].get(mid) if payload else None
        if data is None:
            flash(
                f"{mid}: no response. The change may not have applied.",
                "warning",
            )
        elif isinstance(data, dict) and data.get("result") is False:
            flash(
                f"{mid}/{job_name}: {action} failed: {data.get('comment', 'no detail')}",
                "error",
            )
        else:
            log_event(current_user.username, f"schedule-{action}:{job_name}", minion_id=mid)
            flash(f"{mid}/{job_name}: {action}d.", "success")
    return redirect(url_for("schedules.index", minion=mid))

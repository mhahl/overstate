"""Minion schedules: list, enable/disable/delete per minion. Add waits."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from .auth import roles_required

from .audit import log_event
from .dashboard import get_salt
from .minions import live_roster
from .salt_client import SaltApiError

bp = Blueprint("schedules", __name__, url_prefix="/schedules")

SCHEDULE_ACTIONS = {
    "enable": "schedule.enable_job",
    "disable": "schedule.disable_job",
    "delete": "schedule.delete",
}

SCHEDULE_UNITS = ("seconds", "minutes", "hours", "days")


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
    mid = request.args.get("minion", "")
    client = get_salt()
    statuses, _ = live_roster(client)
    accepted = sorted(m for m, st in statuses.items() if st == "accepted")
    if not mid and accepted:
        mid = accepted[0]
    entries: dict = {}
    raw = ""
    error = None
    if mid:
        try:
            # return_yaml=False keeps this a real mapping: an empty
            # schedule arrives as {} (empty state), not blank YAML text
            # (which the raw fallback would render as a "schedule: {}" box).
            value = client.local(mid, "schedule.list",
                                 kwarg={"return_yaml": False})[0].get(mid, {})
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
    if isinstance(entries, dict):
        key = (lambda kv: kv[0]) if sort == "name" else (
            lambda kv: (str(kv[1].get("function", "")), kv[0]))
        entries = dict(sorted(entries.items(), key=key,
                              reverse=(direction == "desc")))
    return render_template("schedules.html", minions=accepted, mid=mid,
                           entries=entries, raw=raw, error=error,
                           sort=sort, direction=direction)


@bp.post("/<mid>/add")
@roles_required("operator")
def add(mid: str):
    name = request.form.get("name", "").strip()
    fun = request.form.get("function", "").strip()
    unit = request.form.get("unit", "seconds")
    try:
        value = int(request.form.get("value", "").strip())
    except ValueError:
        value = 0
    enabled = request.form.get("enabled", "") == "on"
    if not name or not fun or unit not in SCHEDULE_UNITS or value < 1:
        flash("Name, function, and a positive interval are required.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    client = get_salt()
    try:
        entries = parse_schedule_list(
            client.local(mid, "schedule.list")[0].get(mid, {}))
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
        return redirect(url_for("schedules.index", minion=mid))
    if name in entries:
        flash(f"{mid} already has a scheduled job named '{name}'.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    try:
        result = client.local(mid, "schedule.add", arg=[name],
                              kwarg={"function": fun, unit: value,
                                     "enabled": enabled})
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        if add_succeeded(result, mid):
            log_event(current_user.username, f"schedule-add:{name}")
            flash(f"{mid}/{name}: added.", "success")
        else:
            flash(f"{mid}/{name}: salt did not confirm the add.", "warning")
    return redirect(url_for("schedules.index", minion=mid))


@bp.post("/<mid>/<action>")
@roles_required("operator")
def act(mid: str, action: str):
    if action not in SCHEDULE_ACTIONS:
        flash("Unknown schedule action.", "error")
        return redirect(url_for("schedules.index", minion=mid))
    job_name = request.form.get("job", "")
    try:
        get_salt().local(mid, SCHEDULE_ACTIONS[action], arg=[job_name])
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        log_event(current_user.username, f"schedule-{action}:{job_name}")
        flash(f"{mid}/{job_name}: {action}d.", "success")
    return redirect(url_for("schedules.index", minion=mid))

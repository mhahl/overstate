"""Configurable UTC schedule and status for the file-roots sync CronJob."""

from __future__ import annotations

import datetime as dt

from .audit import log_event
from .db import get_session
from .models import Setting

FILE_SYNC_CRON_DEFAULT = "0 * * * *"
FILE_SYNC_CRON_TICK = "* * * * *"
FILE_SYNC_SETTING = "file_sync_cron"
LAST_RUN_SETTING = "file_sync_last_run"
LAST_STATUS_SETTING = "file_sync_last_status"
LAST_MESSAGE_SETTING = "file_sync_last_message"
LAST_SLOT_SETTING = "file_sync_last_slot"

_FIELDS = (
    (0, 59, "minute"),
    (0, 23, "hour"),
    (1, 31, "day of month"),
    (1, 12, "month"),
    (0, 7, "day of week"),
)


def _field_values(
    token: str, minimum: int, maximum: int, label: str
) -> tuple[set[int], bool]:
    """Expand one numeric cron field and return its values and wildcard flag."""
    values: set[int] = set()
    wildcard = token.startswith("*")
    for item in token.split(","):
        if not item:
            raise ValueError(f"empty item in {label}")
        base, slash, step_text = item.partition("/")
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"invalid step in {label}")
            step = int(step_text)
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, separator, end_text = base.partition("-")
            if not separator or not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid range in {label}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"descending range in {label}")
        elif base.isdigit():
            start = int(base)
            end = maximum if slash else start
        else:
            raise ValueError(f"invalid value in {label}")
        if start < minimum or end > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"{label} has no values")
    # Cron accepts 7 as Sunday as an alias for 0.
    if label == "day of week" and 7 in values:
        values.discard(7)
        values.add(0)
    return values, wildcard


def parse_cron(expression: str) -> tuple[tuple[set[int], bool], ...]:
    """Validate a five-field numeric cron expression.

    Supports ``*``, lists, ranges, and steps (for example
    ``0,30 8-17/2 * * 1-5``). Month/day names and seconds are intentionally
    rejected so the accepted form is unambiguous and dependency-free.
    """
    fields = (expression or "").split()
    if len(fields) != 5:
        raise ValueError("use five cron fields: minute hour day month weekday")
    return tuple(
        _field_values(token, minimum, maximum, label)
        for token, (minimum, maximum, label) in zip(fields, _FIELDS)
    )


def validate_file_sync_schedule(expression: str) -> str:
    """Return a valid file-sync schedule or raise ``ValueError``."""
    value = (expression or "").strip()
    if value.lower() == "off":
        return "off"
    parse_cron(value)
    return value


def cron_matches(expression: str, when: dt.datetime) -> bool:
    """Whether a UTC datetime matches a five-field cron expression."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc)
    minute, hour, dom, month, dow = parse_cron(expression)
    if (
        when.minute not in minute[0]
        or when.hour not in hour[0]
        or when.month not in month[0]
    ):
        return False
    cron_dow = (when.weekday() + 1) % 7
    dom_matches = when.day in dom[0]
    dow_matches = cron_dow in dow[0]
    if dom[1] and dow[1]:
        day_matches = True
    elif dom[1]:
        day_matches = dow_matches
    elif dow[1]:
        day_matches = dom_matches
    else:
        # Standard cron OR semantics when both day fields are restricted.
        day_matches = dom_matches or dow_matches
    return day_matches


def due_slot(expression: str, now: dt.datetime) -> dt.datetime | None:
    """Return the most recent matching minute at or immediately before now.

    The one-minute grace covers a CronJob pod starting just after its
    scheduled minute; old missed executions are not replayed in a burst.
    """
    if (expression or "").strip().lower() == "off":
        return None
    now = now.replace(second=0, microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    for slot in (now, now - dt.timedelta(minutes=1)):
        if cron_matches(expression, slot):
            return slot
    return None


def _set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def _save_status(
    now: dt.datetime,
    status: str,
    message: str,
    slot: dt.datetime | None = None,
) -> None:
    session = get_session()
    _set_setting(session, LAST_RUN_SETTING, now.isoformat())
    _set_setting(session, LAST_STATUS_SETTING, status)
    _set_setting(session, LAST_MESSAGE_SETTING, message[:500])
    if slot is not None:
        _set_setting(session, LAST_SLOT_SETTING, slot.isoformat())
    session.commit()


def run_scheduled_sync(now: dt.datetime | None = None) -> dict:
    """Run one scheduled fast-forward sync, or return a quiet skip result."""
    from .files import _refresh_fileserver
    from .git_sync import git_sync_now
    from .settings import get_setting

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    expression = get_setting(FILE_SYNC_SETTING) or FILE_SYNC_CRON_DEFAULT
    slot = due_slot(expression, now)
    if slot is None:
        return {"ran": False, "ok": True, "message": ""}

    slot_value = slot.isoformat()
    if get_setting(LAST_SLOT_SETTING) == slot_value:
        return {"ran": False, "ok": True, "message": ""}

    previous_status = get_setting(LAST_STATUS_SETTING)
    previous_message = get_setting(LAST_MESSAGE_SETTING)
    result = git_sync_now()
    if not result.get("ok"):
        reason = result.get("reason") or "git sync failed"
        message = f"file sync failed: {reason}"
        _save_status(now, "failed", message)
        log_event("cron", "file-sync:failed", outcome="deny", detail=reason)
        return {"ran": True, "ok": False, "message": message}

    retry_refresh = (
        previous_status == "warning" and "fileserver refresh failed" in previous_message
    )
    refresh_error = None
    if result.get("changed") or retry_refresh:
        refresh_error = _refresh_fileserver()

    old, new = result.get("old") or "unknown", result.get("new") or "unknown"
    if refresh_error:
        status = "warning"
        message = (
            f"file sync pulled {old} -> {new}; fileserver refresh failed: "
            f"{refresh_error}"
        )
        action = "file-sync:refresh-failed"
        log_event("cron", action, outcome="deny", detail=refresh_error[:500])
    elif result.get("changed"):
        status = "success"
        message = f"file sync succeeded: {old} -> {new}; fileserver refreshed"
        log_event("cron", f"file-sync:{new}", outcome="allow")
    else:
        status = "success"
        message = f"file sync succeeded: already up to date at {new}"
        log_event("cron", f"file-sync:up-to-date:{new}", outcome="allow")
    _save_status(now, status, message, slot)
    return {"ran": True, "ok": True, "status": status, "message": message}

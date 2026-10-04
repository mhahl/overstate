"""Browser salt-master CLI console.

Salt-style command lines typed in the browser map onto the same salt-api
calls and guardrails as /jobs/new: the ALLOWED_FUNS allowlist, the
CONFIRM_FUNS type-to-confirm gate, operator role, and an audit row with
the JID. There is no shell here and no SSH: salt-api stays the only
control plane, exactly as PLAN.md requires.
"""

from __future__ import annotations

import shlex

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from .audit import log_event
from .auth import roles_required
from .dashboard import get_salt
from .db import get_session
from .fleet import pod_clients
from .jobs_helpers import (
    ALLOWED_FUNS,
    CONFIRM_FUNS,
    FUN_RE,
    TGT_TYPES,
    is_test_mode,
)
from .jobs_service import launch
from .keys import ACTIONS as KEY_ACTIONS
from .models import JobReturn, Minion
from .salt_client import SaltApiError

bp = Blueprint("console", __name__, url_prefix="/console")

# Runner calls the console may fire: read-only fleet/job inspection.
RUNNER_ALLOW = frozenset(
    {
        "jobs.list_jobs",
        "jobs.lookup_jid",
        "jobs.active",
        "manage.status",
        "manage.versions",
    }
)

_KEY_FLAGS = {
    "-L": "list",
    "--list-all": "list",
    "-a": "accept",
    "--accept": "accept",
    "-r": "reject",
    "--reject": "reject",
    "-d": "delete",
    "--delete": "delete",
}

_KEY_STATUS_ORDER = [
    ("minions", "Accepted"),
    ("minions_pre", "Unaccepted"),
    ("minions_rejected", "Rejected"),
    ("minions_denied", "Denied"),
]

HELP_TEXT = """\
salt-master console — salt commands, same guardrails as Run job.

  salt [-t TYPE] [--sync] [--confirm=TARGET] <target> <function> [args...]
      e.g. salt '*' test.ping
           salt -t grain 'os:Debian' test.ping
           salt '*' state.apply --confirm='*'
      TYPE is one of: glob, list, grain, compound, nodegroup, group.
      Async by default; --sync waits and prints per-minion lines.
      state.* and other confirming functions need --confirm=<exact target>.

  salt-key -L                  list keys on every master pod
  salt-key -a|-r|-d <id>       accept, reject, or delete one key (exact id)

  salt-run <runner> [k=v...]   jobs.list_jobs, jobs.lookup_jid jid=X,
                               jobs.active, manage.status, manage.versions

  help                         this text        clear   wipe the screen

Only functions the Run job form allows will fire. Every firing is
audited with its JID; details stream on the linked job page."""


class ConsoleError(ValueError):
    """A command line the console refuses, with the text to print."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _argv(line: str) -> list[str]:
    try:
        argv = shlex.split(line)
    except ValueError:
        raise ConsoleError("Could not parse that line: unbalanced quotes.") from None
    if not argv:
        raise ConsoleError("Type `help` for the commands this console runs.")
    return argv


def _key_roster() -> tuple[dict[str, list[str]], list[str]]:
    """Union of key ids per status across pods, plus unreachable pod names."""
    roster: dict[str, set[str]] = {key: set() for key, _ in _KEY_STATUS_ORDER}
    unreachable: list[str] = []
    for name, cli in pod_clients(get_salt()):
        try:
            payload = cli.wheel("key.list_all")[0]
            listed = payload.get("data", {}).get("return", payload)
        except (SaltApiError, KeyError, IndexError, TypeError, AttributeError):
            unreachable.append(name)
            continue
        if not isinstance(listed, dict):
            unreachable.append(name)
            continue
        for key, _ in _KEY_STATUS_ORDER:
            ids = listed.get(key) or []
            roster[key].update(str(i) for i in ids)
    return ({k: sorted(v) for k, v in roster.items()}, unreachable)


def _cmd_salt(argv: list[str]) -> dict:
    tgt_type = "glob"
    asynchronous = True
    confirm: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-t", "--tgt-type"):
            i += 1
            if i >= len(argv):
                raise ConsoleError("Missing value: -t/--tgt-type needs one.")
            tgt_type = argv[i]
        elif tok.startswith("--tgt-type="):
            tgt_type = tok.partition("=")[2]
        elif tok == "--sync":
            asynchronous = False
        elif tok == "--async":
            asynchronous = True
        elif tok == "--confirm":
            i += 1
            if i >= len(argv):
                raise ConsoleError("Missing value: --confirm needs the target.")
            confirm = argv[i]
        elif tok.startswith("--confirm="):
            confirm = tok.partition("=")[2]
        elif tok.startswith("-"):
            raise ConsoleError(
                f"Unknown flag {tok}. salt takes -t/--tgt-type, "
                "--sync, --async, --confirm=TARGET."
            )
        else:
            positional.append(tok)
        i += 1
    if len(positional) < 2:
        raise ConsoleError("Usage: salt <target> <function> [args...] — see `help`.")
    tgt, fun, args = positional[0], positional[1], positional[2:]
    if not tgt:
        raise ConsoleError("An empty target never fires.")
    if tgt_type not in TGT_TYPES:
        raise ConsoleError(f"Target type is one of: {', '.join(TGT_TYPES)}.")
    if not FUN_RE.match(fun) or fun not in ALLOWED_FUNS:
        raise ConsoleError(
            f"{fun} cannot run here: only the functions the Run job form "
            "allows will fire (try `salt-run sys.doc` from a real shell, "
            "or pick one in /jobs/new)."
        )
    if fun == "schedule.add":
        for arg in args:
            name, sep, value = arg.partition("=")
            if (
                sep
                and name == "function"
                and (not FUN_RE.match(value) or value not in ALLOWED_FUNS)
            ):
                raise ConsoleError(f"{value} cannot run here.")
    if fun in CONFIRM_FUNS and not is_test_mode(fun, args) and confirm != tgt:
        raise ConsoleError(
            f"{fun} changes the fleet: re-run with --confirm='{tgt}' "
            "to type-to-confirm, exactly like the Run job review."
        )
    try:
        jid = launch(tgt, tgt_type, fun, args, asynchronous)
    except SaltApiError as exc:
        raise ConsoleError(f"salt-api error: {exc}", status=502) from None
    lines = [f"$ salt {tgt} {fun}{' ' if args else ''}{' '.join(args)}".rstrip()]
    if asynchronous:
        lines.append(f"fired {fun} on {tgt} ({tgt_type}); jid: {jid}")
        lines.append("watch returns on the linked job page.")
    else:
        rows = (
            get_session()
            .query(JobReturn)
            .filter_by(jid=jid)
            .order_by(JobReturn.minion_id)
            .all()
        )
        if not rows:
            lines.append(
                "No returns arrived within the wait. They land on the job "
                "page as minions report back."
            )
        shown = rows[:50]
        for row in shown:
            mark = "ok" if row.success else "FAIL"
            payload = (
                "" if isinstance(row.payload, dict) else f" {str(row.payload)[:160]}"
            )
            lines.append(f"{row.minion_id}: {mark}{payload}")
        if len(rows) > len(shown):
            lines.append(f"... and {len(rows) - len(shown)} more; see the job page.")
        lines.append(f"jid: {jid}")
    return {
        "output": "\n".join(lines),
        "jid": jid,
        "job_url": url_for("jobs.detail", jid=jid),
    }


def _cmd_key(argv: list[str]) -> dict:
    if not argv or argv[0] not in _KEY_FLAGS:
        raise ConsoleError("Usage: salt-key -L | salt-key -a|-r|-d <id>.")
    action = _KEY_FLAGS[argv[0]]
    if action == "list":
        roster, unreachable = _key_roster()
        lines = []
        for key, label in _KEY_STATUS_ORDER:
            ids = roster[key]
            lines.append(f"{label} ({len(ids)}):")
            lines.extend(f"  {mid}" for mid in ids[:100])
            if len(ids) > 100:
                lines.append(f"  ... and {len(ids) - 100} more")
        if unreachable:
            lines.append(f"unreachable masters: {', '.join(unreachable)}")
        return {"output": "\n".join(lines)}
    if len(argv) != 2 or not argv[1]:
        raise ConsoleError(
            f"Usage: salt-key {argv[0]} <minion-id> (exact id, no globs)."
        )
    mid = argv[1]
    if any(c in mid for c in "*?[]"):
        raise ConsoleError("Key ids with wildcards are never accepted here.")
    roster, _ = _key_roster()
    known = {i for ids in roster.values() for i in ids}
    if mid not in known:
        raise ConsoleError(f"Unknown key: {mid} is not on the current list.")
    failed = []
    for name, cli in pod_clients(get_salt()):
        try:
            cli.wheel(KEY_ACTIONS[action], match=mid)
        except SaltApiError:
            failed.append(name)
    from .tasks_queue import clear_key_caches

    if failed:
        if len(failed) < len(pod_clients(get_salt())):
            # Reachable masters applied it: drop the roster caches even
            # though this reports as an error.
            clear_key_caches()
        raise ConsoleError(
            f"salt-api error on {', '.join(failed)}: "
            + (
                "nothing changed."
                if len(failed) == len(pod_clients(get_salt()))
                else "partial: other masters applied it."
            ),
            status=502,
        )
    clear_key_caches()
    log_event(current_user.username, f"console:key.{action}:{mid}")
    if action == "delete":
        # Same split-store trap as the Keys page: the minion list unions
        # the snapshot cache with the live roster, so the snapshot row
        # must go too. Job history is kept.
        row = get_session().get(Minion, mid)
        if row is not None:
            get_session().delete(row)
            get_session().commit()
            log_event(current_user.username, f"minion-remove:{mid}")
            return {
                "output": f"{mid}: key.delete applied on every master. "
                "Inventory row removed."
            }
    return {"output": f"{mid}: key.{action} applied on every master."}


def _compact(value, depth: int = 0, budget: list | None = None) -> str:
    """One-screen rendering of a runner return; never dumps raw walls."""
    if budget is None:
        budget = [60]
    if budget[0] <= 0:
        return "..."
    pad = "  " * depth
    if isinstance(value, dict):
        if not value:
            return f"{pad}(empty)"
        lines = []
        for key in sorted(value, key=str)[:25]:
            budget[0] -= 1
            lines.append(
                f"{pad}{key}: {_compact(value[key], depth + 1, budget).lstrip()}"
            )
        if len(value) > 25:
            lines.append(f"{pad}... and {len(value) - 25} more")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        if not value:
            return f"{pad}(empty)"
        lines = []
        for item in list(value)[:25]:
            budget[0] -= 1
            lines.append(f"{pad}- {_compact(item, depth + 1, budget).lstrip()}")
        if len(value) > 25:
            lines.append(f"{pad}... and {len(value) - 25} more")
        return "\n".join(lines)
    text = str(value)
    return f"{pad}{text[:200]}"


def _cmd_runner(argv: list[str]) -> dict:
    if not argv:
        raise ConsoleError("Usage: salt-run <runner> [k=v...] — see `help`.")
    fun = argv[0]
    if fun not in RUNNER_ALLOW:
        raise ConsoleError(
            f"{fun} cannot run here. Allowed runners: "
            + ", ".join(sorted(RUNNER_ALLOW))
            + "."
        )
    kwargs: dict[str, str] = {}
    for tok in argv[1:]:
        name, sep, value = tok.partition("=")
        if not sep or not name:
            raise ConsoleError(f"Runner options are k=v pairs: bad token {tok}.")
        kwargs[name] = value
    if fun in ("manage.status", "manage.versions"):
        # Presence probes gather from the fleet: bound the Salt-side wait
        # (an explicit user timeout still wins) and the HTTP round trip.
        kwargs.setdefault("timeout", 5)
    # An explicit user http_timeout still wins; runner() consumes it as
    # the HTTP cap and never forwards it into the salt-api payload.
    # Garbage falls back to the default instead of 500ing in httpx.
    try:
        kwargs["http_timeout"] = float(kwargs.get("http_timeout", 8))
    except (TypeError, ValueError):
        kwargs["http_timeout"] = 8
    try:
        result = get_salt().runner(fun, **kwargs)[0]
    except SaltApiError as exc:
        raise ConsoleError(f"salt-api error: {exc}", status=502) from None
    except (KeyError, IndexError, TypeError):
        raise ConsoleError("salt-api error: empty reply.", status=502) from None
    log_event(current_user.username, f"console:runner:{fun}")
    return {"output": f"$ salt-run {fun}\n" + _compact(result)}


@bp.get("/")
@login_required
def index():
    return render_template("console.html", help_text=HELP_TEXT)


@bp.post("/run")
@roles_required("operator")
def run():
    data = request.get_json(silent=True) or {}
    line = str(data.get("line", "")).strip()
    if not line:
        return jsonify(
            {"ok": False, "output": "Type `help` for the commands this console runs."}
        ), 400
    try:
        argv = _argv(line)
    except ConsoleError as exc:
        return jsonify({"ok": False, "output": exc.message}), exc.status
    head, rest = argv[0], argv[1:]
    try:
        if head == "help":
            result = {"output": HELP_TEXT}
        elif head == "clear":
            result = {"output": "", "clear": True}
        elif head == "salt":
            result = _cmd_salt(rest)
        elif head == "salt-key":
            result = _cmd_key(rest)
        elif head == "salt-run":
            result = _cmd_runner(rest)
        else:
            raise ConsoleError(
                f"Unknown command {head}: this console runs salt, "
                "salt-key, salt-run, help, clear — nothing else."
            )
    except ConsoleError as exc:
        return jsonify({"ok": False, "output": exc.message}), exc.status
    return jsonify({"ok": True, **result})

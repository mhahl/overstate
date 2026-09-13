"""Job-runner constants and pure helpers (no routes, no Salt calls)."""

from __future__ import annotations

import datetime as dt
import os
import re

TGT_TYPES = ["glob", "list", "grain", "compound", "nodegroup", "group"]
COMPLETE_AFTER_SECONDS = 60
JOB_SORT_COLUMNS = ("started", "jid", "fun", "user")
FUN_RE = re.compile(r"^[A-Za-z0-9_.]+$")
MODS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def sort_jobs(rows: list, sort: str, direction: str) -> list:
    """Sort job rows in place. Started defaults to newest first."""
    reverse = direction == "desc"
    if sort == "fun":
        rows.sort(key=lambda j: (j.fun, j.jid), reverse=reverse)
    elif sort == "user":
        rows.sort(key=lambda j: (j.user, j.jid), reverse=reverse)
    elif sort == "jid":
        rows.sort(key=lambda j: j.jid, reverse=reverse)
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
DESTRUCTIVE_FUNS = frozenset(
    {
        "pkg.install",
        "pkg.remove",
        "service.restart",
        "ps.kill_pid",
    }
)

# Everything gated by the review modal: DESTRUCTIVE_FUNS plus the
# fleet-reconfiguring state runs. Test-mode state runs are exempt.
CONFIRM_FUNS = DESTRUCTIVE_FUNS | {"state.apply", "state.highstate"}


def is_test_mode(fun: str, args: list[str]) -> bool:
    """True for state runs that change nothing (test=True arg)."""
    return fun in ("state.apply", "state.highstate") and any(
        a.lower() == "test=true" for a in args
    )


FLEET_PRESETS = {
    "pkg-install": {"tgt": "*", "tgt_type": "glob", "fun": "pkg.install", "args": ""},
    "pkg-remove": {"tgt": "*", "tgt_type": "glob", "fun": "pkg.remove", "args": ""},
    "service-restart": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "service.restart",
        "args": "",
    },
    "service-status": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "service.status",
        "args": "",
    },
    "process-signal": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "ps.kill_pid",
        "args": "<pid> <signal>",
    },
    "minion-restart": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "service.restart",
        "args": "salt-minion",
    },
    "mine-update": {"tgt": "*", "tgt_type": "glob", "fun": "mine.update", "args": ""},
    "refresh-pillar": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "saltutil.refresh_pillar",
        "args": "",
    },
    "sync-all": {
        "tgt": "*",
        "tgt_type": "glob",
        "fun": "saltutil.sync_all",
        "args": "",
    },
}


OPERATION_GROUPS = [
    (
        "First-class",
        [
            {
                "preset": "ping",
                "fun": "test.ping",
                "args": "",
                "about": "Check minions respond",
            },
            {
                "preset": "apply",
                "fun": "state.apply",
                "args": "",
                "about": "Apply states (args: sls names)",
            },
            {
                "preset": "highstate",
                "fun": "state.highstate",
                "args": "",
                "about": "Enforce the full state tree",
            },
            {
                "preset": "highstate-dry",
                "fun": "state.highstate",
                "args": "test=True",
                "about": "Preview highstate, change nothing",
            },
        ],
    ),
    (
        "Fleet",
        [
            {
                "preset": "pkg-install",
                "fun": "pkg.install",
                "args": "",
                "about": "Install packages (args: names)",
            },
            {
                "preset": "pkg-remove",
                "fun": "pkg.remove",
                "args": "",
                "about": "Remove packages (args: names)",
            },
            {
                "preset": "service-restart",
                "fun": "service.restart",
                "args": "",
                "about": "Restart a service (args: name)",
            },
            {
                "preset": "service-status",
                "fun": "service.status",
                "args": "",
                "about": "Check a service status (args: name)",
            },
            {
                "preset": "process-signal",
                "fun": "ps.kill_pid",
                "args": "<pid> <signal>",
                "about": "Signal a process by PID",
            },
            {
                "preset": "minion-restart",
                "fun": "service.restart",
                "args": "salt-minion",
                "about": "Restart the salt-minion service",
            },
            {
                "preset": "mine-update",
                "fun": "mine.update",
                "args": "",
                "about": "Refresh mine data",
            },
        ],
    ),
    (
        "Pillar & sync",
        [
            {
                "preset": "refresh-pillar",
                "fun": "saltutil.refresh_pillar",
                "args": "",
                "about": "Refresh pillar data",
            },
            {
                "preset": "sync-all",
                "fun": "saltutil.sync_all",
                "args": "",
                "about": "Sync modules to minions",
            },
        ],
    ),
]

_FUN_ABOUT: dict[str, str] = {}
for _group, _ops in OPERATION_GROUPS:
    for _op in _ops:
        _FUN_ABOUT.setdefault(_op["fun"], _op["about"])
OP_FUNCTIONS = [{"fun": fun, "about": about} for fun, about in _FUN_ABOUT.items()]


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


def killable(job) -> bool:
    """A job kill needs a real Salt JID and a minion target."""
    return (
        not job.complete
        and job.tgt_type in ("glob", "list", "grain", "compound", "nodegroup", "group")
        and not job.jid.startswith(("batch-", "ssh-", "sync-"))
    )

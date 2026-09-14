"""Per-domain Salt wrappers run inline or on RQ workers.

Each ``*_now`` function takes a client and never touches the queue;
each ``*_task`` sibling builds an isolated app and delegates. Task
functions must stay importable by path and JSON-serializable in
and out.
"""

from __future__ import annotations

from typing import Any

import httpx

from .salt_client import SaltApiError
from .tasks_queue import isolated_app

CAPABILITY_CHECKS = [
    {
        "key": "wheel_ok",
        "feature": "Keys",
        "fun": "key.list_all",
        "grant": "Grant @wheel to the eauth user",
    },
    {
        "key": "runner_ok",
        "feature": "Presence",
        "fun": "manage.status",
        "grant": "Grant @runner to the eauth user",
    },
    {
        "key": "history_ok",
        "feature": "Job history",
        "fun": "returner tables",
        "grant": "Set master_job_cache: pgjsonb on the master",
    },
    {
        "key": "ping_ok",
        "feature": "Run jobs",
        "fun": "test.ping",
        "grant": "Grant execution functions to the eauth user",
    },
]

FUN_DOC_LINES = 40


def refresh_now(client) -> int:
    """Fleet grains refresh. Shared by the view fallback and the task."""
    from .inventory import refresh_inventory
    from .minions import live_roster

    statuses, _, _ = live_roster(client)
    return refresh_inventory(client, statuses)


def refresh_inventory_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        return {"count": refresh_now(build_client())}


def salt_overview_now(client, http_timeout: float | None = None) -> dict:
    """Key and presence counts. Raises SaltApiError on failure."""
    keys = client.wheel("key.list_all", http_timeout=http_timeout)[0]["data"]["return"]
    accepted = keys.get("minions", [])
    pending = keys.get("minions_pre", [])
    status = client.runner("manage.status", http_timeout=http_timeout)[0]
    return {
        "reachable": True,
        "accepted": len(accepted),
        "pending": len(pending),
        "up": len(status.get("up", [])),
        "down": len(status.get("down", [])),
    }


def salt_overview_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        return salt_overview_now(build_client())


def normalize_versions(payload) -> dict[str, int]:
    """{version: count} from a manage.versions return. Verified live:
    grouped labels map to {minion: version-string} plus a "Master"
    key ({"Up to date": {"m1": "3008.2"}, "Master": "3008.2"}).
    The flat {minion: version} shape counts too. Anything else
    (offline bools, strings, None) yields {} and the caller falls
    back to grain snapshots."""
    counts: dict[str, int] = {}
    if not isinstance(payload, dict):
        return counts
    for key, value in payload.items():
        if key == "Master":
            continue
        if isinstance(value, dict):
            for version in value.values():
                if isinstance(version, str):
                    counts[version] = counts.get(version, 0) + 1
        elif isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return counts


def fleet_truth_now(client, http_timeout: float | None = None) -> dict:
    """Live versions + master-active JIDs. Never raises: each panel
    falls back independently when its call fails or surprises."""
    out: dict = {
        "versions": {},
        "active_jids": [],
        "versions_live": False,
        "active_live": False,
    }
    try:
        versions = normalize_versions(
            client.runner("manage.versions", http_timeout=http_timeout)[0]
        )
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        versions = {}
    if versions:
        out["versions"] = versions
        out["versions_live"] = True
    try:
        active = client.runner("jobs.active", http_timeout=http_timeout)[0]
    except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
        active = None
    # Only JID-shaped keys count: anything else is not a jobs.active
    # payload (e.g. an unexpected dict), so fall back to the DB count
    # instead of reporting a false live zero.
    if isinstance(active, dict) and all(
        isinstance(k, str) and k.isdigit() for k in active
    ):
        out["active_jids"] = sorted(active)
        out["active_live"] = True
    return out


def fleet_truth_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        return fleet_truth_now(build_client())


def list_functions_now(client, minion: str) -> list[str]:
    """Live execution-function index from one minion.

    Raises SaltApiError on failure (caller falls back to presets).
    """
    payload = client.local(minion, "sys.list_functions")[0].get(minion, [])
    names: set[str] = set()
    if isinstance(payload, dict):
        for funs in payload.values():
            if isinstance(funs, list):
                names.update(str(f) for f in funs)
    elif isinstance(payload, list):
        names.update(str(f) for f in payload)
    return sorted(names)


def fun_index_task(minion: str) -> list[str]:
    from .tasks import build_client

    with isolated_app():
        return list_functions_now(build_client(), minion)


def show_sls_now(client, minion: str, sls_list: list[str], via: str = "local") -> dict:
    """Render SLS files via state.show_sls on one minion.

    Returns {sls: {state-id: ...}}. Raises SaltApiError on failure;
    the view degrades to the unavailable note. Tolerates string,
    dict, and None payloads per file.
    """
    rendered: dict = {}
    for sls in sls_list:
        payload = client.local(
            minion, "state.show_sls", arg=[sls], timeout=30, via=via
        )[0].get(minion)
        if isinstance(payload, dict):
            rendered[sls] = payload
    return rendered


def show_sls_task(minion: str, sls_list: list[str], via: str = "local") -> dict:
    from .tasks import build_client

    with isolated_app():
        return show_sls_now(build_client(), minion, sls_list, via)


def mine_get_now(
    client, reader: str, tgt: str, fun: str, tgt_type: str = "glob"
) -> dict:
    """Mine values for a target expression, read through one minion.

    Raises SaltApiError on failure. Non-mapping payloads yield {}
    (caller shows the empty state).
    """
    payload = client.local(
        reader, "mine.get", arg=[tgt, fun], kwarg={"tgt_type": tgt_type}
    )[0].get(reader, {})
    return payload if isinstance(payload, dict) else {}


def mine_get_task(reader: str, tgt: str, fun: str, tgt_type: str = "glob") -> dict:
    from .tasks import build_client

    with isolated_app():
        return mine_get_now(build_client(), reader, tgt, fun, tgt_type)


def fun_doc_now(client, minion: str, fun: str) -> str:
    """Trimmed sys.doc text for one function. Empty when Salt says
    nothing (caller renders the no-docs note)."""
    payload = client.local(minion, "sys.doc", arg=[fun])[0].get(minion, {})
    text = payload.get(fun, "") if isinstance(payload, dict) else payload
    return "\n".join(str(text or "").strip().splitlines()[:FUN_DOC_LINES])


def probe_capabilities(
    client, ping_target: str | None = None, http_timeout: float | None = None
) -> dict:
    """One probe per door the UI depends on. Never raises for Salt."""
    out: dict[str, Any] = {c["key"]: False for c in CAPABILITY_CHECKS}
    out["ping_target"] = ping_target
    out["error"] = None
    try:
        client.wheel("key.list_all", http_timeout=http_timeout)
        out["wheel_ok"] = True
        client.runner("manage.status", http_timeout=http_timeout)
        out["runner_ok"] = True
        from .db import get_session
        from .models import SaltReturn

        get_session().query(SaltReturn.jid).limit(1).all()
        out["history_ok"] = True
        if ping_target:
            client.local(ping_target, "test.ping", http_timeout=http_timeout)
            out["ping_ok"] = True
    except (SaltApiError, httpx.HTTPError) as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — DB problems degrade too
        out["error"] = str(exc)
    return out


def capabilities_task(ping_target: str | None = None) -> dict:
    from .tasks import build_client

    with isolated_app():
        from .tasks_queue import write_capability_cache

        out = probe_capabilities(build_client(), ping_target)
        write_capability_cache(out)
        return out

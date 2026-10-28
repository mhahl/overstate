"""Per-domain Salt wrappers run inline or on RQ workers.

Each ``*_now`` function takes a client and never touches the queue;
each ``*_task`` sibling builds an isolated app and delegates. Task
functions must stay importable by path and JSON-serializable in
and out.
"""

from __future__ import annotations

from typing import Any

import httpx

from .fleet import pod_clients
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
        "fun": "jobs.list_jobs",
        "grant": "Grant @jobs to the eauth user",
    },
    {
        "key": "ping_ok",
        "feature": "Run jobs",
        "fun": "test.ping",
        "grant": "Grant execution functions to the eauth user",
    },
]

FUN_DOC_LINES = 40

FANOUT_HTTP_TIMEOUT = 15.0
"""HTTP backstop for probes that fan out to minions (presence,
versions, capabilities). Healthy fleets answer in a second or two; a
dead minion burns the whole backstop, so master-local probes live in
separate jobs below and stay instant regardless of fleet state."""

PING_SALT_TIMEOUT = 5
"""Salt job timeout for the capability ping door: a dead target fails
fast at the master instead of waiting out the HTTP backstop."""


def refresh_now(client, only_ids: list[str] | None = None) -> int:
    """Fleet grains refresh. Shared by the view fallback and the task."""
    from .inventory import refresh_inventory
    from .minions import live_roster
    from .tasks_queue import write_roster_cache

    statuses, up, _, presence_ok = live_roster(client)
    count = refresh_inventory(client, statuses, only_ids=only_ids)
    if presence_ok:
        write_roster_cache(statuses, up)
    return count


def refresh_inventory_task(
    only_ids: list[str] | None = None, user: str | None = None
) -> dict:
    from .tasks import build_client

    with isolated_app():
        from .audit import log_event
        from .authz import has_fleet, minions_with, rbac_mode
        from .db import get_session
        from .models import User

        if rbac_mode() == "scoped":
            # The worker refuses a ``*`` publish unless the queuing user
            # still holds fleet minion.refresh; a scoped list is
            # re-intersected with their current scope.
            actor = (
                get_session().query(User).filter_by(username=user).first()
                if user
                else None
            )
            if actor is None:
                log_event(user or "unknown", "refresh-denied-authz")
                return {"count": 0, "error": "authorization revoked"}
            if only_ids is None:
                if not has_fleet(actor, "minion.refresh"):
                    log_event(user, "refresh-denied-authz")
                    return {"count": 0, "error": "fleet refresh revoked"}
            else:
                allowed = minions_with(actor, "minion.refresh")
                only_ids = sorted(set(only_ids) & allowed)
        return {"count": refresh_now(build_client(), only_ids=only_ids)}


def fleet_keys_now(client) -> dict:
    """Master-local fleet counts plus active JIDs. Raises SaltApiError
    on failure. Never fans out to minions, so this stays instant no
    matter how many minions are down."""
    keys = client.wheel("key.list_all")[0]["data"]["return"]
    active = client.runner("jobs.active")[0]
    # Only JID-shaped keys count: anything else is not a jobs.active
    # payload (e.g. an unexpected dict), so report no live actives and
    # let the caller fall back to the DB count instead of a false zero.
    live = isinstance(active, dict) and all(
        isinstance(k, str) and k.isdigit() for k in active
    )
    return {
        "reachable": True,
        "accepted": len(keys.get("minions", [])),
        "pending": len(keys.get("minions_pre", [])),
        "active_jids": sorted(active) if live else [],
        "active_live": live,
    }


def fleet_keys_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        # Cluster: union the per-master key buckets and active JIDs —
        # key buckets and publish buses are per-pod, so a single-master
        # count under-reports. Active JIDs are shared via the PG job
        # cache, so the union simply de-duplicates.
        clients = pod_clients(build_client())
        accepted: set[str] = set()
        pending: set[str] = set()
        active: set[str] = set()
        answered = 0
        shaped = 0
        for _name, cli in clients:
            try:
                keys = cli.wheel("key.list_all")[0]["data"]["return"]
                live = cli.runner("jobs.active")[0]
            except (SaltApiError, KeyError, IndexError, TypeError):
                continue
            answered += 1
            accepted.update(keys.get("minions", []) or [])
            pending.update(keys.get("minions_pre", []) or [])
            if isinstance(live, dict) and all(
                isinstance(k, str) and k.isdigit() for k in live
            ):
                shaped += 1
                active.update(live)
        if not answered:
            raise SaltApiError("no master reachable")
        return {
            "reachable": True,
            "accepted": len(accepted),
            "pending": len(pending),
            "active_jids": sorted(active),
            "active_live": shaped == answered,
        }


def fleet_presence_now(client, http_timeout: float | None = None) -> dict:
    """Up/down presence. Raises SaltApiError on failure. Fans out to
    minions, so the runner args are bounded hard: manage.status waits
    out the master timeout for every missing returner, so without
    timeout=1/gather_job_timeout=1 one dead minion would stretch every
    presence stat past the HTTP backstop."""
    status = client.runner(
        "manage.status",
        http_timeout=http_timeout,
        timeout=1,
        gather_job_timeout=1,
    )[0]
    return {
        "reachable": True,
        "up": len(status.get("up", [])),
        "down": len(status.get("down", [])),
    }


def fleet_presence_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        # Cluster: union live minions across every master (each pod's
        # manage.status lists only the minions attached to it). Down is
        # accepted keys with no live return on any pod, never a
        # single-pod under-count — a minion attached to pod-1 appears
        # "down" everywhere else.
        clients = pod_clients(build_client())
        up: set[str] = set()
        accepted: set[str] = set()
        answered = 0
        for _name, cli in clients:
            try:
                status = cli.runner(
                    "manage.status",
                    http_timeout=FANOUT_HTTP_TIMEOUT,
                    timeout=1,
                    gather_job_timeout=1,
                )[0]
                keys = cli.wheel("key.list_all")[0]["data"]["return"]
            except (SaltApiError, KeyError, IndexError, TypeError):
                continue
            if not isinstance(status, dict):
                continue
            answered += 1
            up.update(status.get("up") or [])
            accepted.update(keys.get("minions", []) or [])
        if not answered:
            raise SaltApiError("no master reachable")
        down = accepted - up
        return {
            "reachable": True,
            "up": len(up),
            "down": len(down),
        }


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


def fleet_versions_now(client, http_timeout: float | None = None) -> dict:
    """Live version counts. Raises SaltApiError on failure. Fans out to
    minions; an empty result means no minion reported a version, so the
    caller keeps the grain-snapshot counts."""
    versions = normalize_versions(
        client.runner("manage.versions", http_timeout=http_timeout)[0]
    )
    return {"versions": versions}


def fleet_versions_task() -> dict:
    from .tasks import build_client

    with isolated_app():
        # Cluster: manage.versions reports each pod's attached minions,
        # so the fleet counts are the sum across masters. The shared PG
        # job cache means no risk of double counting a returner.
        clients = pod_clients(build_client())
        counts: dict[str, int] = {}
        answered = 0
        for _name, cli in clients:
            try:
                sub = normalize_versions(
                    cli.runner("manage.versions", http_timeout=FANOUT_HTTP_TIMEOUT)[0]
                )
            except (SaltApiError, KeyError, IndexError, TypeError):
                continue
            answered += 1
            for version, n in sub.items():
                counts[version] = counts.get(version, 0) + n
        if not answered:
            raise SaltApiError("no master reachable")
        return {"versions": counts}


def list_functions_now(client, minion: str) -> list[str]:
    """Live execution-function index from one minion.

    Raises SaltApiError on failure (caller falls back to presets).
    """
    payload = client.local(minion, "sys.list_functions", tgt_type="list")[0].get(
        minion, []
    )
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
            minion, "state.show_sls", arg=[sls], timeout=30, via=via, tgt_type="list"
        )[0].get(minion)
        if isinstance(payload, dict):
            rendered[sls] = payload
    return rendered


def show_sls_task(minion: str, sls_list: list[str], via: str = "local") -> dict:
    from .tasks import build_client

    with isolated_app():
        return show_sls_now(build_client(), minion, sls_list, via)


def show_highstate_now(client, minion: str, via: str = "local") -> dict:
    """One-shot live highstate description for a single minion.

    Display-only: the caller shows it next to the stored return and
    never writes it into job history. Raises SaltApiError on failure;
    the view degrades to stored data plus an advisory note.
    """
    payload = client.local(minion, "state.show_highstate", via=via, tgt_type="list")[
        0
    ].get(minion)
    return payload if isinstance(payload, dict) else {}


def show_highstate_task(
    minion: str, via: str = "local", user: str | None = None
) -> dict:
    from .tasks import build_client

    with isolated_app():
        from .authz import authorize, rbac_mode
        from .db import get_session
        from .models import User

        if rbac_mode() == "scoped":
            actor = (
                get_session().query(User).filter_by(username=user).first()
                if user
                else None
            )
            if actor is None or not authorize(actor, "pillar.read", minion=minion):
                return {}
        return show_highstate_now(build_client(), minion, via)


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
    payload = client.local(minion, "sys.doc", arg=[fun], tgt_type="list")[0].get(
        minion, {}
    )
    text = payload.get(fun, "") if isinstance(payload, dict) else payload
    return "\n".join(str(text or "").strip().splitlines()[:FUN_DOC_LINES])


def probe_capabilities(
    client, ping_target: str | None = None, http_timeout: float | None = None
) -> dict:
    """One probe per door the UI depends on. Never raises for Salt, and
    each door reports independently: a slow or denied wheel door must
    not blank the runner, history, or ping doors behind it. The history
    door asks the master's job cache (jobs.list_jobs) — Postgres-side
    freshness has its own Returns signal on the dashboard."""
    out: dict[str, Any] = {c["key"]: False for c in CAPABILITY_CHECKS}
    out["ping_target"] = ping_target
    out["error"] = None

    def attempt(key: str, func) -> None:
        try:
            func()
        except (SaltApiError, httpx.HTTPError) as exc:
            if out["error"] is None:
                out["error"] = str(exc)
        else:
            out[key] = True

    attempt("wheel_ok", lambda: client.wheel("key.list_all", http_timeout=http_timeout))
    attempt(
        "runner_ok",
        # Bounded like fleet_presence_now: manage.status waits out the
        # master timeout for missing returners, so without runner args
        # a dead minion would stall the capability probe as well.
        lambda: client.runner(
            "manage.status",
            http_timeout=http_timeout,
            timeout=1,
            gather_job_timeout=1,
        ),
    )
    attempt(
        "history_ok",
        lambda: client.runner("jobs.list_jobs", http_timeout=http_timeout),
    )
    if ping_target:
        attempt(
            "ping_ok",
            lambda: client.local(
                ping_target,
                "test.ping",
                timeout=PING_SALT_TIMEOUT,
                http_timeout=http_timeout,
                tgt_type="list",
            ),
        )
    return out


def capabilities_task(ping_target: str | None = None) -> dict:
    from .tasks import build_client

    with isolated_app():
        from .tasks_queue import write_capability_cache

        out = probe_capabilities(
            build_client(), ping_target, http_timeout=FANOUT_HTTP_TIMEOUT
        )
        write_capability_cache(out)
        return out

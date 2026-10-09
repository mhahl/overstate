"""Minion inventory helpers: roster merge, grains, onboarding, beacons.

Pure functions and their constants; the routes stay in
:mod:`overstate_ui.minions`.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

OS_ICONS = (
    ("fedora", "fedora"),
    ("opensuse", "opensuse"),
    ("suse", "opensuse"),
    ("ubuntu", "ubuntu"),
    ("debian", "debian"),
    ("centos", "centos"),
    ("red hat", "redhat"),
    ("rhel", "redhat"),
    ("alma", "almalinux"),
    ("rocky", "rockylinux"),
    ("arch", "archlinux"),
    ("gentoo", "gentoo"),
    ("freebsd", "freebsd"),
    ("windows", "windows"),
    ("macos", "apple"),
    ("darwin", "apple"),
)

PRESENCE_ORDER = {"up": 0, "dead": 1, "down": 2}
ONBOARD_DISTROS = ("rhel", "fedora", "suse")
ONBOARD_INSTALL = {
    "rhel": "dnf -y install salt-minion",
    "fedora": "dnf -y install salt-minion",
    "suse": "zypper --non-interactive install salt-minion",
}
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def os_icon_slug(grains: dict) -> str:
    """Simple Icons slug for the minion OS; generic Linux fallback."""
    hay = f"{grains.get('os', '')} {grains.get('osfinger', '')}".lower()
    for needle, slug in OS_ICONS:
        if needle in hay:
            return slug
    return "linux"


def presence_of(row: dict) -> str:
    if row["up"]:
        return "up"
    if row["dead"]:
        return "dead"
    return "down"


def parse_beacon_list(value) -> dict:
    """Parse beacons.list output. With ``return_yaml=False`` this is a
    mapping of beacon name to config; anything else (a YAML string on
    older renders, None on an empty set) yields {} and the caller
    shows the raw payload or the empty state instead."""
    if isinstance(value, dict):
        return dict(value)
    return {}


_INTERVAL_UNITS = (
    ("days", "day"),
    ("hours", "hour"),
    ("minutes", "minute"),
    ("seconds", "second"),
)


def _interval_text(entry: dict) -> str:
    parts = []
    for key, singular in _INTERVAL_UNITS:
        try:
            value = int(entry.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            parts.append(f"{value} {singular if value == 1 else key}")
    return "every " + ", ".join(parts) if parts else ""


def summarize_schedule(sched) -> tuple[bool | None, list[dict]]:
    """Summarize ``schedule.list`` output into table rows.

    Returns ``(scheduler_enabled, rows)``; each row has ``name``,
    ``function``, ``every`` (human schedule text), ``arguments``
    (compact ``job_args``/``job_kwargs``) and ``enabled``. The top
    level ``enabled`` key is scheduler state, not a job, so it is
    skipped. Non-mapping payloads (older YAML-string renders) yield
    ``(None, [])`` and the caller shows the raw payload instead.
    """
    if not isinstance(sched, dict):
        return None, []
    rows = []
    for name, entry in sched.items():
        if name == "enabled" or not isinstance(entry, dict):
            continue
        every = _interval_text(entry)
        if not every:
            if entry.get("cron"):
                every = f"cron {entry['cron']}"
            elif entry.get("when"):
                every = f"at {entry['when']}"
            elif entry.get("once") or entry.get("once_fmt"):
                every = f"once {(entry.get('once_fmt') or entry.get('once') or '')}".rstrip()
            else:
                every = "no schedule set"
        if entry.get("splay"):
            every += f" (+{entry['splay']}s splay)"
        bits = []
        if entry.get("job_args"):
            bits.append(json.dumps(entry["job_args"], default=str))
        if entry.get("job_kwargs"):
            bits.append(json.dumps(entry["job_kwargs"], default=str))
        rows.append(
            {
                "name": name,
                "function": entry.get("function") or "",
                "every": every,
                "arguments": " ".join(bits),
                "enabled": entry.get("enabled", True),
            }
        )
    rows.sort(key=lambda r: r["name"])
    enabled = sched.get("enabled")
    return (bool(enabled) if isinstance(enabled, bool) else None), rows


ROSTER_HTTP_TIMEOUT = 8.0
"""HTTP backstop for the roster reads behind the minion pages. Healthy
masters answer in well under a second; a sick one must degrade to the
snapshot cache fast instead of pinning a sync worker for the 30 s
client default — on every filter keystroke, no less."""


def live_roster(
    client, http_timeout: float | None = ROSTER_HTTP_TIMEOUT
) -> tuple[dict[str, str], set[str], bool]:
    """Return ({minion_id: key_status}, {up minion ids}, reachable).

    Offline-safe: each half fails independently and logs. Reachable is
    true when either call succeeds, even with zero minions — an empty
    fleet is not an outage, and the banner must not claim one.
    """
    from .salt_client import SaltApiError

    statuses: dict[str, str] = {}
    up: set[str] = set()
    keys_ok = False
    presence_ok = False
    try:
        listed = client.wheel("key.list_all", http_timeout=http_timeout)[0]["data"][
            "return"
        ]
        for mid in listed.get("minions", []):
            statuses[mid] = "accepted"
        for mid in listed.get("minions_pre", []):
            statuses[mid] = "pending"
        for mid in listed.get("minions_rejected", []):
            statuses[mid] = "rejected"
        for mid in listed.get("minions_denied", []):
            statuses[mid] = "denied"
    except (SaltApiError, KeyError, IndexError, TypeError) as exc:
        # The minions page degrades to the snapshot cache on empty output,
        # so log the cause here: otherwise the banner is the only evidence.
        logger.warning("live key list unavailable: %s", exc)
    else:
        keys_ok = True
    try:
        up = set(
            client.runner("manage.status", http_timeout=http_timeout)[0].get("up", [])
        )
    except (SaltApiError, KeyError, IndexError, TypeError) as exc:
        logger.warning("live presence unavailable: %s", exc)
    else:
        presence_ok = True
    return statuses, up, keys_ok or presence_ok


def normalize_grains(grains) -> dict:
    """Coerce snapshot/live grains for display. Proxy minions and older
    releases omit keys or report scalars (e.g. a single ipv4 string)."""
    if not isinstance(grains, dict):
        return {}
    grains = dict(grains)
    ipv4 = grains.get("ipv4")
    if isinstance(ipv4, str):
        grains["ipv4"] = [ipv4]
    elif not isinstance(ipv4, list):
        grains["ipv4"] = []
    return grains


def minion_entries(
    statuses: dict,
    up: set,
    q: str,
    status_filter: str,
    sort: str = "id",
    direction: str = "asc",
) -> list[dict]:
    """Lightweight per-minion merge: id, key_status, up, dead, last_seen.

    Same merge, filter, and sort semantics as :func:`minion_rows` but
    loads no grains: one narrow column scan instead of full snapshot
    JSON per minion. Callers hydrate only the ids they render (see
    :func:`hydrate_entries`), so listing a fleet costs a page of grains,
    not the whole table.
    """
    from .db import get_session
    from .models import Minion

    session = get_session()
    if sort == "os":
        # osfinger ordering needs grains for the filtered set; every
        # other sort orders from the narrow columns alone.
        query = session.query(
            Minion.id, Minion.key_status, Minion.last_seen, Minion.grains
        )
    else:
        query = session.query(Minion.id, Minion.key_status, Minion.last_seen)
    by_id: dict[str, dict] = {}
    for row in query.order_by(Minion.id).all():
        by_id[row[0]] = {
            "id": row[0],
            "key_status": row[1],
            "up": False,
            "dead": False,
            "last_seen": row[2],
            "grains": row[3] if sort == "os" else {},
        }
    for mid, st in statuses.items():
        by_id.setdefault(
            mid,
            {
                "id": mid,
                "key_status": st,
                "up": False,
                "dead": False,
                "last_seen": None,
                "grains": {},
            },
        )
        by_id[mid]["key_status"] = st
    rows = []
    for row in by_id.values():
        row["up"] = row["id"] in up
        row["dead"] = row["key_status"] == "accepted" and not row["up"] and bool(up)
        if q and q not in row["id"]:
            continue
        if status_filter and row["key_status"] != status_filter:
            continue
        rows.append(row)
    if sort == "key":
        key = lambda r: (r["key_status"], r["id"])
    elif sort == "presence":
        key = lambda r: (PRESENCE_ORDER[presence_of(r)], r["id"])
    elif sort == "os":
        key = lambda r: (row_grains(r).get("osfinger", ""), r["id"])
    else:
        key = lambda r: r["id"]
    rows.sort(key=key, reverse=(direction == "desc"))
    if sort != "os":
        for row in rows:
            row.pop("grains", None)
    return rows


def row_grains(row: dict) -> dict:
    """Raw snapshot grains for a lightweight entry ({} when absent)."""
    grains = row.get("grains")
    return grains if isinstance(grains, dict) else {}


def hydrate_entries(entries: list[dict]) -> list[dict]:
    """Attach normalized snapshot grains to lightweight entries.

    One IN query for exactly the ids being rendered. Live-only ids
    (keys never snapshotted) hydrate to {} like before.
    """
    if not entries:
        return []
    from .db import get_session
    from .models import Minion

    _missing = object()
    ids = [e["id"] for e in entries]
    grains_by_id = {
        mid: grains
        for mid, grains in get_session()
        .query(Minion.id, Minion.grains)
        .filter(Minion.id.in_(ids))
        .all()
    }
    out = []
    for entry in entries:
        row = dict(entry)
        row.pop("grains", None)
        # Live-only ids (keys never snapshotted) historically hydrate
        # to a bare {} — not normalized — so keep that exact shape.
        raw = grains_by_id.get(row["id"], _missing)
        row["grains"] = normalize_grains(raw) if raw is not _missing else {}
        out.append(row)
    return out


def minion_rows(
    statuses: dict,
    up: set,
    q: str,
    status_filter: str,
    sort: str = "id",
    direction: str = "asc",
) -> list[dict]:
    """Full rows with the historical contract: filter, sort, hydrate all.

    The CSV export uses this; paged views combine :func:`minion_entries`
    with :func:`hydrate_entries` on the visible slice instead.
    """
    return hydrate_entries(
        minion_entries(statuses, up, q, status_filter, sort, direction)
    )


def minion_is_up(client, mid: str) -> bool | None:
    """Whether mid is in the live up set.

    None when the roster itself was unreachable — callers must fail
    open (keep the live call) and never read absence-from-a-failed-read
    as "down".
    """
    _, up, reachable = cached_roster(client)
    if not reachable:
        return None
    return mid in up


def cached_roster(
    client, http_timeout: float | None = ROSTER_HTTP_TIMEOUT
) -> tuple[
    dict[str, str],
    set[str],
    bool,
]:
    """(statuses, up, reachable) from the roster cache when warm.

    Falls back to a live read and refreshes the cache when reachable.
    Total outages are never cached, so recovery shows up on the next
    view instead of at TTL expiry.
    """
    from .tasks_queue import read_roster_cache, write_roster_cache

    cached = read_roster_cache()
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("statuses"), dict)
        and isinstance(cached.get("up"), list)
    ):
        return dict(cached["statuses"]), set(cached["up"]), True
    statuses, up, reachable = live_roster(client, http_timeout=http_timeout)
    if reachable:
        write_roster_cache(statuses, up)
    return statuses, up, reachable


def refresh_sync(only_ids: list[str] | None = None) -> None:
    """Synchronous refresh. Worker fallback and no-Redis path."""
    from flask import flash

    from .dashboard import get_salt
    from .inventory import refresh_inventory
    from .salt_client import SaltApiError
    from .tasks_queue import write_roster_cache

    client = get_salt()
    try:
        # Always live: this is the explicit Refresh button, and its
        # fresh statuses repopulate the roster cache for the views.
        statuses, up, _ = live_roster(client)
        count = refresh_inventory(client, statuses, only_ids=only_ids)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        write_roster_cache(statuses, up)
        flash(f"Inventory refreshed: {count} minions.", "success")


def build_onboard_script(
    distro: str, topology: str, masters: tuple[str, ...], mid: str
) -> str:
    """Render a root-run minion installer from validated hostname inputs."""
    if distro == "suse":
        repo_path = "/etc/zypp/repos.d/overstate-saltproject.repo"
        repo_config = """[overstate-saltproject]
name=Salt Project (Broadcom)
enabled=1
autorefresh=1
baseurl=https://packages.broadcom.com/artifactory/saltproject-rpm/
type=rpm-md
gpgcheck=1
gpgkey=https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public
"""
        refresh = (
            "zypper --gpg-auto-import-keys --non-interactive "
            "refresh overstate-saltproject"
        )
    else:
        repo_path = "/etc/yum.repos.d/overstate-saltproject.repo"
        repo_config = """[overstate-saltproject]
name=Salt Project (Broadcom)
baseurl=https://packages.broadcom.com/artifactory/saltproject-rpm/
enabled=1
gpgcheck=1
gpgkey=https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public
"""
        refresh = ""

    if topology == "failover":
        master_config = "master:\n" + "".join(f'  - "{master}"\n' for master in masters)
        master_config += "master_type: failover\nmaster_alive_interval: 30\n"
    else:
        master_config = f'master: "{masters[0]}"\n'

    return f"""#!/bin/sh
# Generated by Overstate for {mid}.
set -eu
[ "$(id -u)" -eq 0 ] || {{ echo "Run with sudo: curl ... | sudo sh" >&2; exit 1; }}
cat > {repo_path} <<'REPO'
{repo_config}
REPO
{refresh}
{ONBOARD_INSTALL[distro]}
install -d -m 0755 /etc/salt/minion.d
cat > /etc/salt/minion.d/overstate.conf <<'MINION'
{master_config}id: "{mid}"
MINION
systemctl enable --now salt-minion
echo "Minion '{mid}' started. Accept its key in Overstate > Keys, then run test.ping."
"""


def onboard_inputs(args) -> tuple[str, str, tuple[str, ...], str] | None:
    """Validated (distro, topology, masters, mid), or None when
    the form was not submitted or failed validation (caller flashes)."""
    if "mid" not in args and "masters" not in args and "master" not in args:
        return None
    from flask import flash

    from .settings import get_setting

    mid = args.get("mid", "").strip()
    distro = args.get("distro", "suse")
    topology = args.get("topology", "single")
    raw_masters = args.get("masters", args.get("master", get_setting("master_host")))
    masters = tuple(
        master.strip() for master in re.split(r"[\s,]+", raw_masters) if master.strip()
    )
    if (
        not mid
        or distro not in ONBOARD_DISTROS
        or not HOST_RE.match(mid)
        or topology not in ("single", "failover")
        or (topology == "single" and len(masters) != 1)
        or (
            topology == "failover"
            and (
                len(masters) < 2
                or len({master.lower() for master in masters}) != len(masters)
            )
        )
        or any(not HOST_RE.match(master) for master in masters)
    ):
        flash(
            "Minion id and masters must be valid hostnames; choose a valid setup and distribution.",
            "error",
        )
        return None
    return (distro, topology, masters, mid)


def _beacon_refusal(outcome, mid: str) -> str | None:
    """Salt's own refusal text when a toggle changed nothing.

    Pillar-defined beacons answer ``{mid: {comment, result: False}}``
    with HTTP 200, so a missing exception means nothing here: surface
    the comment instead of flashing success.
    """
    if isinstance(outcome, list) and outcome and isinstance(outcome[0], dict):
        ret = outcome[0].get(mid)
        if isinstance(ret, dict) and ret.get("result") is False:
            return str(ret.get("comment") or "toggle changed nothing.")
    return None

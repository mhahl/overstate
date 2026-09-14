"""Minion inventory helpers: roster merge, grains, onboarding, beacons.

Pure functions and their constants; the routes stay in
:mod:`overstate_ui.minions`.
"""

from __future__ import annotations

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
ONBOARD_DISTROS = ("opensuse", "fedora")
ONBOARD_INSTALL = {
    "opensuse": "sudo zypper -n install salt-minion",
    "fedora": "sudo dnf -y install salt-minion",
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


def live_roster(client) -> tuple[dict[str, str], set[str], bool]:
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
        listed = client.wheel("key.list_all")[0]["data"]["return"]
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
        up = set(client.runner("manage.status")[0].get("up", []))
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


def minion_rows(
    statuses: dict,
    up: set,
    q: str,
    status_filter: str,
    sort: str = "id",
    direction: str = "asc",
) -> list[dict]:
    from .db import get_session
    from .models import Minion

    session = get_session()
    by_id: dict[str, dict] = {}
    for row in session.query(Minion).order_by(Minion.id).all():
        by_id[row.id] = {
            "id": row.id,
            "key_status": row.key_status,
            "up": False,
            "dead": False,
            "last_seen": row.last_seen,
            "grains": normalize_grains(row.grains),
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
        key = lambda r: (r["grains"].get("osfinger", ""), r["id"])
    else:
        key = lambda r: r["id"]
    rows.sort(key=key, reverse=(direction == "desc"))
    return rows


def refresh_sync() -> None:
    """Synchronous refresh. Worker fallback and no-Redis path."""
    from flask import flash

    from .dashboard import get_salt
    from .inventory import refresh_inventory
    from .salt_client import SaltApiError

    client = get_salt()
    try:
        statuses, _, _ = live_roster(client)
        count = refresh_inventory(client, statuses)
    except SaltApiError as exc:
        flash(f"salt-api error: {exc}", "error")
    else:
        flash(f"Inventory refreshed: {count} minions.", "success")


def build_onboard_script(distro: str, master: str, mid: str) -> str:
    """Render a join script for a new minion. Inputs must already match
    HOST_RE / ONBOARD_DISTROS; values are shell-safe by construction."""
    install = ONBOARD_INSTALL[distro]
    return (
        "#!/bin/sh\n"
        f"# Generated by Overstate: joins this machine to '{master}' as '{mid}'.\n"
        "# Review, then run as root on the new minion.\n"
        "set -eu\n"
        f"{install}\n"
        f"printf 'master: {master}\\nid: {mid}\\n'"
        " | sudo tee /etc/salt/minion > /dev/null\n"
        "sudo systemctl enable --now salt-minion\n"
        f"echo \"Minion '{mid}' started. Accept its key in Overstate > Keys, then run test.ping.\"\n"
    )


def onboard_inputs(args) -> tuple[str, str, str] | None:
    """Validated (distro, master, mid) from the wizard form, or None when
    the form was not submitted or failed validation (caller flashes)."""
    if "mid" not in args and "master" not in args:
        return None
    from flask import flash

    from .settings import get_setting

    mid = args.get("mid", "").strip()
    distro = args.get("distro", "opensuse")
    master = args.get("master", get_setting("master_host")).strip()
    if (
        not mid
        or distro not in ONBOARD_DISTROS
        or not HOST_RE.match(mid)
        or not master
        or not HOST_RE.match(master)
    ):
        flash(
            "Minion id and master must be valid hostnames; pick a distribution.",
            "error",
        )
        return None
    return (distro, master, mid)


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

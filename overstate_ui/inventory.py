"""Inventory snapshots. Salt is the source of truth; the minions table is
a cache refreshed on demand (button) or by an RQ worker when one runs."""

from __future__ import annotations

import datetime as dt

from .db import get_session
from .models import Minion

GRAIN_COLUMNS = [
    "osfinger",
    "osrelease",
    "fqdn",
    "cpuarch",
    "num_cpus",
    "mem_total",
    "virtual",
    "saltversion",
]

SNAPSHOT_GRAINS = (
    "os",
    "osfinger",
    "osrelease",
    "fqdn",
    "ipv4",
    "cpuarch",
    "num_cpus",
    "mem_total",
    "virtual",
    "saltversion",
    "proxytype",
    "proxyid",
)
"""Grain keys worth snapshotting. The list, CSV, detail header, OS icon,
and proxy badge read exactly these; full ``grains.items`` payloads
carry dozens more (cpu_flags and friends) that multiply every
fleet-wide refresh for data nothing renders. The detail page still
pulls full live grains for a minion that answers, so "all grain facts"
stays complete whenever the minion is up."""


def refresh_inventory(client, key_statuses: dict[str, str]) -> int:
    """Pull the snapshot grains fleet-wide and upsert the cache.

    Structured so an RQ worker can call it by import path; the refresh
    button calls it synchronously (dev fleets are tiny).
    """
    now = dt.datetime.now(dt.UTC)
    result = client.local("*", "grains.item", arg=list(SNAPSHOT_GRAINS), timeout=30)[0]
    session = get_session()
    count = 0
    for mid, grains in result.items():
        if not isinstance(grains, dict):
            continue
        row = session.get(Minion, mid)
        if row is None:
            row = Minion(id=mid, grains={}, conformity={})
            session.add(row)
        row.grains = grains
        row.last_seen = now
        row.key_status = key_statuses.get(mid, row.key_status or "accepted")
        count += 1
    session.commit()
    return count

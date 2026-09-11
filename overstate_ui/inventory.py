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


def refresh_inventory(client, key_statuses: dict[str, str]) -> int:
    """Pull grains.items fleet-wide and upsert the snapshot cache.

    Structured so an RQ worker can call it by import path; the refresh
    button calls it synchronously (dev fleets are tiny).
    """
    now = dt.datetime.now(dt.timezone.utc)
    result = client.local("*", "grains.items", timeout=30)[0]
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

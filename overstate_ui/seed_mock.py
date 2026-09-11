"""Mock-data seeder for local UI testing. Never runs against Salt.

Usage:
    python -m overstate_ui.seed_mock [--force]

Reads DATABASE_URL (defaults to the dev-stack postgres). Refuses to run when
the tables already hold data unless --force is given, in which case mock rows
are replaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .models import (
    AuditEvent,
    Base,
    Job,
    JobReturn,
    Minion,
    SavedJob,
    Setting,
    WatchedState,
)

NOW = dt.datetime.now(dt.timezone.utc)


def _grains(osfinger: str, osrelease: str, ip: str, salt_version: str) -> dict:
    return {
        "osfinger": osfinger,
        "osrelease": osrelease,
        "fqdn": f"{ip.replace('.', '-')}.lab.example",
        "ipv4": [ip, "127.0.0.1"],
        "cpuarch": "x86_64",
        "num_cpus": 4,
        "mem_total": 16384,
        "virtual": "kvm",
        "saltversion": salt_version,
        "pkg_manager": "zypper" if "SUSE" in osfinger else "dnf",
    }


def mock_minions() -> list[Minion]:
    return [
        Minion(
            id="tw-master-01",
            key_status="accepted",
            last_seen=NOW - dt.timedelta(minutes=2),
            grains=_grains("openSUSE Tumbleweed", "20260901", "10.0.0.11", "3006.5"),
            conformity={"status": "ok", "jid": "20260910120000000001"},
        ),
        Minion(
            id="tw-minion-02",
            key_status="accepted",
            last_seen=NOW - dt.timedelta(minutes=5),
            grains=_grains("openSUSE Leap 15.6", "15.6", "10.0.0.12", "3006.1"),
            conformity={"status": "drifted", "jid": "20260910120000000001"},
        ),
        Minion(
            id="fedora-web-01",
            key_status="accepted",
            last_seen=NOW - dt.timedelta(minutes=1),
            grains=_grains("Fedora Linux 41", "41", "10.0.0.21", "3006.5"),
            conformity={"status": "ok", "jid": "20260910120000000001"},
        ),
        Minion(
            id="fedora-db-01",
            key_status="accepted",
            last_seen=NOW - dt.timedelta(hours=3),
            grains=_grains("Fedora Linux 40", "40", "10.0.0.22", "3006.0"),
            conformity={"status": "unreachable", "jid": "20260910090000000000"},
        ),
        Minion(
            id="new-node-01",
            key_status="pending",
            last_seen=None,
            grains={},
            conformity={"status": "unknown"},
        ),
    ]


def mock_jobs() -> tuple[list[Job], list[JobReturn]]:
    ping = Job(
        jid="20260910120000000001",
        fun="test.ping",
        tgt="*",
        tgt_type="glob",
        user="admin",
        started_at=NOW - dt.timedelta(hours=1),
        complete=True,
    )
    highstate = Job(
        jid="20260910123000000002",
        fun="state.highstate",
        tgt="*",
        tgt_type="glob",
        user="admin",
        started_at=NOW - dt.timedelta(minutes=30),
        complete=True,
    )
    running = Job(
        jid="20260910124500000003",
        fun="state.apply",
        tgt="fedora-*",
        tgt_type="glob",
        user="admin",
        started_at=NOW - dt.timedelta(minutes=2),
        complete=False,
    )
    returns = [
        JobReturn(jid=ping.jid, minion_id="tw-master-01", success=True, retcode=0,
                  payload=True),
        JobReturn(jid=ping.jid, minion_id="fedora-web-01", success=True, retcode=0,
                  payload=True),
        JobReturn(jid=highstate.jid, minion_id="tw-master-01", success=True,
                  retcode=0, payload={"succeeded": 42, "failed": 0}),
        JobReturn(jid=highstate.jid, minion_id="tw-minion-02", success=False,
                  retcode=1, payload={"succeeded": 40, "failed": 2,
                                      "failures": ["pkg_|-nginx_|-nginx_|-installed"]}),
    ]
    return [ping, highstate, running], returns


def seed(session: Session, force: bool = False) -> dict[str, int]:
    """Insert mock rows. Raises RuntimeError if data exists and not force."""
    existing = session.query(Minion).count() + session.query(Job).count()
    if existing and not force:
        raise RuntimeError(
            f"tables already hold {existing} minion/job rows; pass --force to replace"
        )
    for model in (JobReturn, Job, Minion, AuditEvent, SavedJob, WatchedState, Setting):
        session.query(model).delete()
    jobs, returns = mock_jobs()
    session.add_all(mock_minions())
    session.add_all(jobs)
    session.add_all(returns)
    session.add_all(
        [
            SavedJob(name="ping everything", fun="test.ping", tgt="*",
                     tgt_type="glob", args=[]),
            SavedJob(name="dry-run highstate", fun="state.highstate", tgt="*",
                     tgt_type="glob", args=["test=True"]),
            WatchedState(sls="common"),
            WatchedState(sls="baseline"),
            Setting(key="default_target", value="*"),
            Setting(key="page_size", value="25"),
            Setting(key="theme", value="light"),
            AuditEvent(user="admin", action="accept-key", jid=None),
            AuditEvent(user="admin", action="state.highstate",
                       jid="20260910123000000002"),
        ]
    )
    session.commit()
    return {"minions": 5, "jobs": 3, "returns": 4}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed mock data for local testing")
    parser.add_argument("--force", action="store_true",
                        help="replace existing mock rows")
    args = parser.parse_args(argv)
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://overstate:overstate@localhost:5432/overstate",
    )
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            counts = seed(session, force=args.force)
    except RuntimeError as exc:
        print(f"seed_mock: {exc}", file=sys.stderr)
        return 1
    print("seeded: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

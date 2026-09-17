"""Exit 0 when this Salt master sees a Raft leader it belongs to.

Used by the salt-master readiness path (the watcher writes
``.cluster_ready`` when this succeeds). Stdlib only. ``salt-run`` is
optional at import time so unit tests can drive ``is_ready`` without a
daemon.

A founder with ``leader_id == self`` is ready even if the voter list is
still catching up (OrderedReady: pod-0 must become Ready before pod-1
starts). A joiner is ready only once it appears in voters or learners
under a non-null leader.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


class ReadyError(Exception):
    """Status is missing, unreadable, or shows no usable leader."""


def is_ready(doc, self_id):
    """Return True if *doc* (cluster.members JSON) means this node can serve."""
    if not isinstance(doc, dict) or not self_id:
        return False
    leader = doc.get("leader_id")
    if not leader:
        return False
    if leader == self_id:
        return True
    members = list(doc.get("voters") or []) + list(doc.get("learners") or [])
    return self_id in members


def _parse_members(stdout):
    """Decode salt-run JSON, skipping any log prefix lines."""
    text = (stdout or "").strip()
    if not text:
        raise ReadyError("empty cluster.members")
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ReadyError("cluster.members not JSON")
        try:
            return json.loads(text[start : end + 1])
        except ValueError as exc:
            raise ReadyError("cluster.members not JSON") from exc


def members_doc(env=None):
    """Run ``salt-run cluster.members --out=json``; raise ReadyError."""
    salt_run = (env or os.environ).get("SALT_RUN", "salt-run")
    try:
        proc = subprocess.run(
            [salt_run, "cluster.members", "--out=json", "--log-level=quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=env or os.environ,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadyError(f"salt-run failed: {exc}") from exc
    if proc.returncode != 0:
        raise ReadyError(f"salt-run exit {proc.returncode}")
    return _parse_members(proc.stdout)


def main(argv, env=None):
    self_id = argv[1] if len(argv) > 1 else env.get("NODE_NAME") if env else None
    if not self_id:
        raise ReadyError("no node id")
    if not is_ready(members_doc(env), self_id):
        raise ReadyError("no leader for this node")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv, os.environ))
    except ReadyError as exc:
        print(f"cluster-ready: {exc}", file=sys.stderr)
        sys.exit(1)

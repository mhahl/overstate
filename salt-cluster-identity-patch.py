"""Redirect Salt's cluster node identity from pod IP to a stable DNS name.

Upstream Salt 3008's experimental cluster keys Raft membership (and the
join sentinel, founder election, and ring ownership) on
``opts["interface"]`` — the pod IP, which Kubernetes changes on every
recreate. The committed voter set then keeps dead IPs, quorum is lost
forever, and every master defers all minion/API traffic with
``cluster_retry`` (salt-api 401s, minion pillar/auth flap) until an
operator wipes the Raft state on all pods at once.

This patcher rewrites the handful of identity sites to prefer a new
free-form ``cluster_node_id`` opt (stamped by cluster-entrypoint.sh as
``<pod-name>.<headless-service>``, a DNS name that survives recreates)
and fall back to ``interface`` when it is unset, so unpatched behavior
is byte-for-byte identical without the opt.

Vehicle: exact-string replacement with anchor verification, applied at
image build time to the installed Salt tree. Any anchor that does not
occur exactly once fails the build loudly instead of silently shipping
a half-patched tree (this is coupled to the base image's Salt version
by design — see Containerfile.salt-master).

Usage:
  python3 salt-cluster-identity-patch.py [salt-root]
  (default salt-root: directory containing the imported ``salt`` package)
"""

from __future__ import annotations

import pathlib
import sys

SELF_NODE_ID = '(self.opts.get("cluster_node_id") or self.opts["interface"])'

# (relative path, exact old text, exact new text). Each old text must
# occur exactly once in its file; anything else is a hard error.
PATCHES = [
    (
        "salt/cluster/consensus/service.py",
        """\
        # Use the interface address as the Raft node-id so it matches the
        # keys in cluster_peers and the peer_pushers dict.  opts["id"] is
        # the hostname which remote masters do not share; the interface
        # address is the consistent cluster-wide identity.
        node_id = opts["interface"]
""",
        """\
        # The stable cluster_node_id (a DNS name that survives pod
        # recreates) is the Raft node-id so it matches the keys in
        # cluster_peers and the peer_pushers dict.  Falls back to the
        # interface address (upstream default) when unset.
        node_id = opts.get("cluster_node_id") or opts["interface"]
""",
    ),
    (
        "salt/cluster/consensus/service.py",
        """\
        return SaltPeer(
            addr,
            pusher,
            self.opts["interface"],
            voting=voting,
            raft_group_id=raft_group_id,
        )
""",
        f"""\
        return SaltPeer(
            addr,
            pusher,
            {SELF_NODE_ID},
            voting=voting,
            raft_group_id=raft_group_id,
        )
""",
    ),
    (
        "salt/channel/server.py",
        """\
    if not opts.get("cluster_id"):
        return True
    import salt.master  # pylint: disable=import-outside-toplevel

    entry = salt.master.SMaster.secrets.get("cluster_ready")
    if entry is None:
        return False
    return entry["event"].is_set()
""",
        """\
    if not opts.get("cluster_id"):
        return True
    import salt.master  # pylint: disable=import-outside-toplevel

    entry = salt.master.SMaster.secrets.get("cluster_ready")
    if entry is not None and entry["event"].is_set():
        return True
    # Python 3.14 defaults to forkserver: MWorkers do not inherit
    # SMaster.secrets. PubServer writes <cachedir>/health/ready when
    # this node is a committed voter; honor that so pillar is not
    # deferred forever (minion: "Master did not return a session key").
    try:
        import salt.cluster.healthchecks as _hc

        ready = _hc.health_dir(opts)
        if ready is not None and (ready / _hc.READY_SENTINEL).is_file():
            return True
    except Exception:
        pass
    return False
""",
    ),
    (
        "salt/master.py",
        """\
                if not ipc_publisher._has_joined_cluster() and not is_founder:
                    log.info("No cluster join sentinel — running cluster discover/join")
                    join_event = multiprocessing.Event()
                    ipc_publisher._discover_event = join_event
                    ipc_publisher.discover_peers()
""",
        """\
                _join_existing = False
                if is_founder and not ipc_publisher._has_joined_cluster():
                    import socket as _sock

                    _port = int(
                        self.opts.get("cluster_port")
                        or self.opts.get("cluster_pool_port")
                        or 4507
                    )
                    _self = self.opts.get("cluster_node_id") or self.opts["interface"]
                    for _peer in self.opts.get("cluster_peers") or []:
                        if _peer == _self:
                            continue
                        try:
                            _s = _sock.create_connection((_peer, _port), timeout=2)
                            _s.close()
                            _join_existing = True
                            log.info(
                                "Founder saw live peer %s:%s — joining, not founding",
                                _peer,
                                _port,
                            )
                            break
                        except OSError:
                            pass
                if not ipc_publisher._has_joined_cluster() and (
                    not is_founder or _join_existing
                ):
                    log.info("No cluster join sentinel — running cluster discover/join")
                    join_event = multiprocessing.Event()
                    ipc_publisher._discover_event = join_event
                    ipc_publisher.discover_peers()
""",
    ),
    (
        "salt/channel/server.py",
        """\
                if bootstrap_pool and bootstrap_pool[0] == self.opts["interface"]:
                    log.info(
                        "New node bootstrapping cluster %r as designated founder",
                        self.opts["cluster_id"],
                    )
""",
        """\
                _self_id = self.opts.get("cluster_node_id") or self.opts["interface"]
                _is_founder = bool(bootstrap_pool) and bootstrap_pool[0] == _self_id
                _join_existing = False
                if _is_founder:
                    import socket as _sock

                    _port = int(
                        self.opts.get("cluster_port")
                        or self.opts.get("cluster_pool_port")
                        or 4507
                    )
                    for _peer in self.opts.get("cluster_peers") or []:
                        if _peer == _self_id:
                            continue
                        try:
                            _s = _sock.create_connection((_peer, _port), timeout=2)
                            _s.close()
                            _join_existing = True
                            log.info(
                                "Founder saw live peer %s:%s — joining, not founding",
                                _peer,
                                _port,
                            )
                            break
                        except OSError:
                            pass
                if _is_founder and not _join_existing:
                    log.info(
                        "New node bootstrapping cluster %r as designated founder",
                        self.opts["cluster_id"],
                    )
""",
    ),
    (
        "salt/channel/server.py",
        """\
        interface = self.opts.get("interface") or "unknown"
        return pathlib.Path(self.opts["cachedir"]) / f".cluster_joined.{interface}"
""",
        """\
        node_id = self.opts.get("cluster_node_id") or self.opts.get("interface") or "unknown"
        return pathlib.Path(self.opts["cachedir"]) / f".cluster_joined.{node_id}"
""",
    ),
    (
        "salt/channel/server.py",
        """\
                bootstrap_pool = sorted(
                    {self.opts["interface"], *self.opts.get("cluster_peers", [])}
                )
""",
        f"""\
                bootstrap_pool = sorted(
                    {{{SELF_NODE_ID}, *self.opts.get("cluster_peers", [])}}
                )
""",
    ),
    (
        "salt/master.py",
        """\
                bootstrap_pool = sorted(
                    {self.opts["interface"], *self.opts.get("cluster_peers", [])}
                )
""",
        f"""\
                bootstrap_pool = sorted(
                    {{{SELF_NODE_ID}, *self.opts.get("cluster_peers", [])}}
                )
""",
    ),
    (
        "salt/master.py",
        "bootstrap_pool[0] == self.opts[\"interface\"]",
        f"bootstrap_pool[0] == {SELF_NODE_ID}",
    ),
    (
        "salt/cluster/ring_membership.py",
        """\
    node_id = opts.get("interface")
    if node_id is None:
        # Defensive: opts without an interface (some test fixtures)
""",
        """\
    node_id = opts.get("cluster_node_id") or opts.get("interface")
    if node_id is None:
        # Defensive: opts without an interface (some test fixtures)
""",
    ),
    (
        "salt/cluster/ring_membership.py",
        """\
    node_id = opts.get("interface")
    if node_id is None:
        return True
    if ring.owns(key, node_id):
""",
        """\
    node_id = opts.get("cluster_node_id") or opts.get("interface")
    if node_id is None:
        return True
    if ring.owns(key, node_id):
""",
    ),
]


class PatchError(Exception):
    """An anchor did not match exactly once (upstream drift)."""


def apply_patches(root, patches=PATCHES):
    """Apply every (relpath, old, new); return files changed. Raise PatchError."""
    root = pathlib.Path(root)
    changed = []
    for relpath, old, new in patches:
        path = root / relpath
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PatchError(f"{relpath}: cannot read: {exc}") from exc
        count = text.count(old)
        if count != 1:
            raise PatchError(
                f"{relpath}: anchor occurs {count}x (expected exactly once); "
                "base Salt version drifted — update salt-cluster-identity-patch.py"
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
        changed.append(relpath)
    return changed


def salt_root():
    """Directory containing the installed ``salt`` package."""
    import salt

    return pathlib.Path(salt.__file__).resolve().parent.parent


def main(argv):
    root = pathlib.Path(argv[1]) if len(argv) > 1 else salt_root()
    changed = apply_patches(root)
    for relpath in changed:
        print(f"patched {relpath}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except PatchError as exc:
        print(f"salt-cluster-identity-patch: {exc}", file=sys.stderr)
        sys.exit(1)

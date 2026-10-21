"""Apply Overstate's surgical fixes to an installed Salt cluster tree.

Upstream Salt 3008's experimental cluster keys Raft membership (and the
join sentinel, founder election, and ring ownership) on
``opts["interface"]`` — the pod IP, which Kubernetes changes on every
recreate. The committed voter set then keeps dead IPs, quorum is lost
forever, and every master defers all minion/API traffic with
``cluster_retry`` (salt-api 401s, minion pillar/auth flap) until an
operator wipes the Raft state on all pods at once.

This patcher ships every cluster fix the deployment needs, not just
identity: stable DNS node ids, per-master localfs Raft storage,
forkserver-safe readiness, founder join-existing probes, membership
reconcile replay, and transport recovery. Identity sites prefer a new
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
  python3 patch-salt-cluster.py [--check] [--list] [salt-root]
  (default salt-root: directory containing the imported ``salt`` package)
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass

# Stable node identity expression, reused by every replacement that asks
# "who am I" in Raft / ring / founder code. The DNS name survives pod
# recreates; ``interface`` (the pod IP) stays the upstream default.
SELF_NODE_ID = '(self.opts.get("cluster_node_id") or self.opts["interface"])'


@dataclass(frozen=True)
class Patch:
    """One anchored source replacement in the installed Salt tree."""

    relpath: str
    anchor: str
    replacement: str
    purpose: str  # shown in --list and error messages


PATCHES: tuple[Patch, ...] = (
    Patch(
        "salt/cluster/consensus/storage.py",
        """\
Implements :class:`salt.cluster.consensus.raft.BaseStorage` using the
``salt.cache.Cache`` abstraction so that whatever ``cache_driver`` the
operator has configured (``localfs`` today, ``mmapcache`` tomorrow) is used
automatically.
""",
        """\
Implements :class:`salt.cluster.consensus.raft.BaseStorage` using the
``localfs`` cache driver for durable per-master Raft state. The global Salt
``cache:`` option is reserved for minion data and may point to shared Redis;
it must not redirect consensus logs to a shared cache.
""",
        "module docstring: Raft state stays on the per-master PVC (localfs), never the shared cache",
    ),
    Patch(
        "salt/cluster/consensus/storage.py",
        """\
        self._cache = salt.cache.Cache(opts)
""",
        """\
        # Raft state belongs to this master's PVC. Keep it on localfs even
        # when Salt's global cache driver is changed for shared minion data.
        # The durability helper below fsyncs the localfs log and metadata.
        self._cache = salt.cache.Cache(opts, driver="localfs")
""",
        "pin SaltStorage to the localfs driver regardless of the global cache: opt",
    ),
    Patch(
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
        "Raft node-id is the stable cluster_node_id (DNS name), not the pod IP",
    ),
    Patch(
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
        "SaltPeer advertises the same stable local id as the Raft node-id",
    ),
    Patch(
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
        "forkserver-safe cluster readiness: fall back to the health-file sentinel",
    ),
    Patch(
        "salt/master.py",
        """\
                if not ipc_publisher._has_joined_cluster() and not is_founder:
                    log.info("No cluster join sentinel — running cluster discover/join")
                    join_event = multiprocessing.Event()
                    ipc_publisher._discover_event = join_event
                    ipc_publisher.discover_peers()
""",
        f"""\
                _join_existing = False
                if is_founder and not ipc_publisher._has_joined_cluster():
                    import socket as _sock

                    _port = int(
                        self.opts.get("cluster_port")
                        or self.opts.get("cluster_pool_port")
                        or 4507
                    )
                    _self = {SELF_NODE_ID}
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
        "founder joins an existing cluster instead of founding a second one",
    ),
    Patch(
        "salt/channel/server.py",
        """\
                if bootstrap_pool and bootstrap_pool[0] == self.opts["interface"]:
                    log.info(
                        "New node bootstrapping cluster %r as designated founder",
                        self.opts["cluster_id"],
                    )
""",
        f"""\
                _self_id = {SELF_NODE_ID}
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
        "founder join-existing probe on the publish-server path",
    ),
    Patch(
        "salt/channel/server.py",
        """\
        interface = self.opts.get("interface") or "unknown"
        return pathlib.Path(self.opts["cachedir"]) / f".cluster_joined.{interface}"
""",
        """\
        node_id = self.opts.get("cluster_node_id") or self.opts.get("interface") or "unknown"
        return pathlib.Path(self.opts["cachedir"]) / f".cluster_joined.{node_id}"
""",
        "join sentinel is keyed by stable node id, not the pod IP",
    ),
    Patch(
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
        "bootstrap_pool is built from the stable node id, not the pod IP",
    ),
    Patch(
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
        "bootstrap_pool is built from the stable node id, not the pod IP",
    ),
    Patch(
        "salt/master.py",
        'bootstrap_pool[0] == self.opts["interface"]',
        f"bootstrap_pool[0] == {SELF_NODE_ID}",
        "founder comparison uses the stable node id, not the pod IP",
    ),
    Patch(
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
        "ring ownership identity prefers cluster_node_id",
    ),
    Patch(
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
        "ring.owns() identity prefers cluster_node_id",
    ),
    Patch(
        "salt/cluster/consensus/raft/node.py",
        """\
        if self.membership_sm is None:
            return
        voters = self.membership_sm.current_voters()
        learners = self.membership_sm.current_learners()
        if not voters and not learners:
            # Nothing committed yet (e.g. fresh node before founding CONFIG).
            return
""",
        """\
        if self.membership_sm is None:
            return
        # Snapshot restore can predate the latest committed CONFIG
        # entries: replay them so a rejoining node sees the committed
        # view (same replay the cluster.members runner performs).
        # Without this, a restart with an empty snapshot wedges
        # reconcile as a no-op and on_change (cluster-ready) never
        # fires, leaving the master deferring all traffic.
        for entry in getattr(getattr(self, "log", None), "entries", None) or []:
            if getattr(entry, "type", None) == LogEntryType.CONFIG:
                self.membership_sm.apply(entry.cmd, index=entry.index)
        voters = self.membership_sm.current_voters()
        learners = self.membership_sm.current_learners()
        if not voters and not learners:
            # Nothing committed yet (e.g. fresh node before founding CONFIG).
            return
""",
        "reconcile replays committed CONFIG entries after snapshot restore",
    ),
    Patch(
        "salt/cluster/consensus/peer.py",
        """\
        client = _TCPPubServerPublisher(
            pusher.pull_host, pusher.pull_port, getattr(pusher, "pull_path", None)
        )
        await client.connect()
        # Stash on the pusher so the next send reuses the connection.
        # The pusher's lifetime exceeds the master process; the kernel
        # closes the fd at exit, so explicit teardown isn't required.
        pusher._consensus_async_client = client
    await client.send(raw)
""",
        """\
        client = _TCPPubServerPublisher(
            pusher.pull_host, pusher.pull_port, getattr(pusher, "pull_path", None)
        )
        await client.connect()
        # Stash on the pusher so the next send reuses the connection.
        # The pusher's lifetime exceeds the master process; the kernel
        # closes the fd at exit, so explicit teardown isn't required.
        pusher._consensus_async_client = client
    try:
        await client.send(raw)
    except Exception:
        # A dead cached stream (peer pod restarted, RST mid-handshake)
        # must not poison this pusher for the life of the process:
        # drop the cached client so the next send reconnects fresh
        # (re-resolving the peer). The caller logs the dropped RPC.
        # Deliberately no retry here: retrying a possibly-delivered
        # fire-and-forget publish could duplicate Raft RPCs, and
        # duplicate pre-vote replies poison candidacies (CandidacyError)
        # and stall elections.
        try:
            client.close()
        except Exception:
            pass
        try:
            del pusher._consensus_async_client
        except AttributeError:
            pass
        raise
""",
        "drop a dead cached Raft client on send failure; reconnect on the next send, never retry",
    ),
)


class PatchError(Exception):
    """An anchor did not match exactly once (upstream drift)."""


def _group_by_file(patches: Sequence[Patch]) -> dict[str, list[Patch]]:
    """Keep patch order per file so replacements can chain in sequence."""
    grouped: dict[str, list[Patch]] = {}
    for patch in patches:
        grouped.setdefault(patch.relpath, []).append(patch)
    return grouped


def apply_patches(root, patches: Sequence[Patch] = PATCHES, dry_run: bool = False) -> list[str]:
    """Apply every patch; return the patched relpaths. Raise PatchError.

    Files are read once and written once after all of their patches
    validate, so a failing anchor never leaves a half-patched file.
    With ``dry_run=True`` every anchor is still verified, but nothing
    is written — use it to validate a new base image.
    """
    root = pathlib.Path(root)
    changed = []
    for relpath, file_patches in _group_by_file(patches).items():
        path = root / relpath
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PatchError(f"{relpath}: cannot read: {exc}") from exc
        for patch in file_patches:
            count = text.count(patch.anchor)
            if count != 1:
                raise PatchError(
                    f"{relpath} [{patch.purpose}]: anchor occurs {count}x "
                    "(expected exactly once); base Salt version drifted — "
                    "update patch-salt-cluster.py"
                )
            text = text.replace(patch.anchor, patch.replacement)
        if not dry_run:
            path.write_text(text, encoding="utf-8")
        changed.append(relpath)
    return changed


def salt_root():
    """Directory containing the installed ``salt`` package."""
    import salt

    return pathlib.Path(salt.__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patch-salt-cluster",
        description="Apply Overstate's surgical fixes to an installed Salt cluster tree.",
    )
    parser.add_argument("salt_root", nargs="?", help="Salt install root (default: derived from the imported salt package)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify every anchor matches exactly once without writing anything",
    )
    parser.add_argument("--list", action="store_true", help="list the patch set and exit")
    args = parser.parse_args(argv)

    if args.list:
        for index, patch in enumerate(PATCHES, 1):
            print(f"{index:2}. {patch.relpath} — {patch.purpose}")
        return 0

    root = pathlib.Path(args.salt_root) if args.salt_root else salt_root()
    changed = apply_patches(root, dry_run=args.check)
    verb = "would patch" if args.check else "patched"
    for relpath in changed:
        print(f"{verb} {relpath}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as exc:
        print(f"patch-salt-cluster: {exc}", file=sys.stderr)
        sys.exit(1)

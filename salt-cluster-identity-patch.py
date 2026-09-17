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
        "salt/channel/server.py",
        "bootstrap_pool[0] == self.opts[\"interface\"]",
        f"bootstrap_pool[0] == {SELF_NODE_ID}",
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

"""salt-cluster-identity-patch.py tests: exact-once anchored replacement.

The patcher rewrites upstream Salt's interface-keyed cluster identity
to the stable `cluster_node_id` opt at image build time. These tests
exercise the application logic against synthetic trees (the real Salt
tree only exists inside the image build); anchor drift against a new
base Salt version fails the build loudly via PatchError, which is the
production safety net and is covered here as a contract.
"""

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PATCH_PY = REPO / "salt-cluster-identity-patch.py"


def _load():
    spec = importlib.util.spec_from_file_location("identity_patch", PATCH_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path, files):
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_applies_each_patch_exactly_once(tmp_path):
    mod = _load()
    files = {}
    for rel, old, _new in mod.PATCHES:
        files.setdefault(rel, "")
        files[rel] += f"# prefix {rel}\n{old}# suffix\n"
    _tree(tmp_path, files)
    changed = mod.apply_patches(tmp_path)
    assert len(changed) == len(mod.PATCHES)
    assert sorted(set(changed)) == sorted({rel for rel, _, _ in mod.PATCHES})
    for rel, old, new in mod.PATCHES:
        text = (tmp_path / rel).read_text(encoding="utf-8")
        assert old not in text
        assert text.count(new) >= 1


def test_missing_anchor_is_a_hard_error(tmp_path):
    mod = _load()
    rel, old, new = mod.PATCHES[0]
    _tree(tmp_path, {rel: "# nothing to patch here\n"})
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [(rel, old, new)])


def test_duplicate_anchor_is_a_hard_error(tmp_path):
    mod = _load()
    rel, old, new = mod.PATCHES[0]
    _tree(tmp_path, {rel: f"{old}\n{old}\n"})
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [(rel, old, new)])


def test_missing_file_is_a_hard_error(tmp_path):
    mod = _load()
    rel, old, new = mod.PATCHES[0]
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [(rel, old, new)])


def test_transport_patch_drops_dead_client_without_retry(tmp_path):
    """Fix 1: a failed send clears the cached client and re-raises.

    No retry by design — retrying a possibly-delivered fire-and-forget
    publish could duplicate Raft RPCs and poison candidacies.
    """
    mod = _load()
    entry = next(e for e in mod.PATCHES if e[0] == "salt/cluster/consensus/peer.py")
    _rel, old, new = entry
    assert "await client.send(raw)" in old
    assert "del pusher._consensus_async_client" in new
    assert "raise" in new.split("del pusher._consensus_async_client")[1]
    retry_markers = ("await client.send(raw)",)
    assert new.count(retry_markers[0]) == 1, "must send exactly once (no retry)"
    _tree(tmp_path, {_rel: f"# prefix\n{old}# suffix\n"})
    mod.apply_patches(tmp_path, [entry])
    text = (tmp_path / _rel).read_text(encoding="utf-8")
    assert old not in text and "del pusher._consensus_async_client" in text


def test_reconcile_replays_post_snapshot_config(tmp_path):
    """Fix 2: reconcile_membership replays CONFIG entries after restore.

    A restart whose snapshot predates the latest committed CONFIG
    (observed: empty snapshot, 3-voter config in log entries) left
    reconcile as a no-op, so on_change (cluster-ready) never fired
    and the master deferred all traffic with cluster_retry.
    """
    mod = _load()
    entry = next(
        e for e in mod.PATCHES if e[0] == "salt/cluster/consensus/raft/node.py"
    )
    _rel, old, new = entry
    assert "current_voters()" in old
    assert "LogEntryType.CONFIG" in new
    assert "membership_sm.apply(entry.cmd, index=entry.index)" in new
    assert "current_voters()" in new
    _tree(tmp_path, {_rel: f"# prefix\n{old}# suffix\n"})
    mod.apply_patches(tmp_path, [entry])
    text = (tmp_path / _rel).read_text(encoding="utf-8")
    assert old not in text and "LogEntryType.CONFIG" in text


def test_patch_table_is_well_formed():
    mod = _load()
    assert mod.PATCHES, "no patches declared"
    files = set()
    for entry in mod.PATCHES:
        rel, old, new = entry
        assert rel.endswith(".py") and not rel.startswith("/")
        assert len(old.strip()) > 20, "anchor too short to be safe"
        assert old != new
        assert (
            "cluster_node_id" in new
            or "READY_SENTINEL" in new
            or "joining, not founding" in new
            or "_consensus_async_client" in new
            or "replay" in new
        ), (
            f"{rel}: replacement must use cluster_node_id, ready sentinel, join-existing, transport recovery, or log replay"
        )
        files.add(rel)
    # Raft identity, join sentinel + founder, and ring ownership.
    assert "salt/cluster/consensus/service.py" in files
    assert "salt/channel/server.py" in files
    assert "salt/master.py" in files
    assert "salt/cluster/ring_membership.py" in files

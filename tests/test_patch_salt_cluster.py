"""patch-salt-cluster.py tests: exact-once anchored replacement.

The patcher surgically fixes upstream Salt's experimental cluster code
at image build time: stable `cluster_node_id` identity, per-master
localfs Raft storage, forkserver-safe readiness, founder join-existing,
reconcile replay, and transport recovery. These tests exercise the
application logic against synthetic trees (the real Salt tree only
exists inside the image build); anchor drift against a new base Salt
version fails the build loudly via PatchError, which is the production
safety net and is covered here as a contract.
"""

import importlib.util
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PATCH_PY = REPO / "patch-salt-cluster.py"


def _load():
    spec = importlib.util.spec_from_file_location("patch_salt_cluster", PATCH_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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
    for patch in mod.PATCHES:
        files.setdefault(patch.relpath, "")
        files[patch.relpath] += f"# prefix {patch.relpath}\n{patch.anchor}# suffix\n"
    _tree(tmp_path, files)
    changed = mod.apply_patches(tmp_path)
    assert set(changed) == {patch.relpath for patch in mod.PATCHES}
    for patch in mod.PATCHES:
        text = (tmp_path / patch.relpath).read_text(encoding="utf-8")
        assert patch.anchor not in text
        assert text.count(patch.replacement) >= 1


def test_missing_anchor_is_a_hard_error(tmp_path):
    mod = _load()
    patch = mod.PATCHES[0]
    _tree(tmp_path, {patch.relpath: "# nothing to patch here\n"})
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [patch])


def test_duplicate_anchor_is_a_hard_error(tmp_path):
    mod = _load()
    patch = mod.PATCHES[0]
    _tree(tmp_path, {patch.relpath: f"{patch.anchor}\n{patch.anchor}\n"})
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [patch])


def test_missing_file_is_a_hard_error(tmp_path):
    mod = _load()
    patch = mod.PATCHES[0]
    with pytest.raises(mod.PatchError):
        mod.apply_patches(tmp_path, [patch])


def test_error_names_the_patch_purpose(tmp_path):
    mod = _load()
    patch = mod.PATCHES[0]
    _tree(tmp_path, {patch.relpath: "# nothing to patch here\n"})
    with pytest.raises(mod.PatchError, match=re.escape(patch.purpose)):
        mod.apply_patches(tmp_path, [patch])


def test_dry_run_verifies_without_writing(tmp_path):
    mod = _load()
    files = {}
    for patch in mod.PATCHES:
        files.setdefault(patch.relpath, "")
        files[patch.relpath] += f"# prefix {patch.relpath}\n{patch.anchor}# suffix\n"
    _tree(tmp_path, files)
    changed = mod.apply_patches(tmp_path, dry_run=True)
    assert set(changed) == {patch.relpath for patch in mod.PATCHES}
    for patch in mod.PATCHES:
        text = (tmp_path / patch.relpath).read_text(encoding="utf-8")
        assert patch.anchor in text, "dry run must not modify files"


def test_apply_is_grouped_per_file_single_write(tmp_path):
    mod = _load()
    relpath = "salt/channel/server.py"
    patches = [p for p in mod.PATCHES if p.relpath == relpath]
    assert len(patches) >= 4, "channel/server.py carries multiple patches"
    body = "".join(f"# prefix\n{p.anchor}# suffix\n" for p in patches)
    _tree(tmp_path, {relpath: body})
    changed = mod.apply_patches(tmp_path, patches)
    assert changed == [relpath], "one patched file must be reported once"


def test_transport_patch_drops_dead_client_without_retry(tmp_path):
    """Fix 1: a failed send clears the cached client and re-raises.

    No retry by design — retrying a possibly-delivered fire-and-forget
    publish could duplicate Raft RPCs and poison candidacies.
    """
    mod = _load()
    entry = next(p for p in mod.PATCHES if p.relpath == "salt/cluster/consensus/peer.py")
    _rel, old, new = entry.relpath, entry.anchor, entry.replacement
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
        p for p in mod.PATCHES if p.relpath == "salt/cluster/consensus/raft/node.py"
    )
    _rel, old, new = entry.relpath, entry.anchor, entry.replacement
    assert "current_voters()" in old
    assert "LogEntryType.CONFIG" in new
    assert "membership_sm.apply(entry.cmd, index=entry.index)" in new
    assert "current_voters()" in new
    _tree(tmp_path, {_rel: f"# prefix\n{old}# suffix\n"})
    mod.apply_patches(tmp_path, [entry])
    text = (tmp_path / _rel).read_text(encoding="utf-8")
    assert old not in text and "LogEntryType.CONFIG" in text


def test_raft_storage_uses_per_master_localfs_not_global_cache(tmp_path):
    """Raft persistence must stay on each master's PVC when minion data uses Redis."""
    mod = _load()
    entry = next(
        (
            p
            for p in mod.PATCHES
            if p.relpath == "salt/cluster/consensus/storage.py"
            and 'driver="localfs"' in p.replacement
        ),
        None,
    )
    assert entry is not None, "Raft storage must not follow Salt's global cache driver"
    rel, old, new = entry.relpath, entry.anchor, entry.replacement
    assert 'salt.cache.Cache(opts, driver="localfs")' in new
    _tree(tmp_path, {rel: f"# prefix\n{old}# suffix\n"})
    mod.apply_patches(tmp_path, [entry])
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert old not in text
    assert 'salt.cache.Cache(opts, driver="localfs")' in text


def test_patch_table_is_well_formed():
    mod = _load()
    assert mod.PATCHES, "no patches declared"
    files = set()
    for patch in mod.PATCHES:
        rel, old, new = patch.relpath, patch.anchor, patch.replacement
        assert rel.endswith(".py") and not rel.startswith("/")
        assert len(old.strip()) > 20, "anchor too short to be safe"
        assert old != new
        assert patch.purpose, "every patch must state its purpose"
        assert (
            "cluster_node_id" in new
            or "READY_SENTINEL" in new
            or "joining, not founding" in new
            or "_consensus_async_client" in new
            or "replay" in new
            or 'driver="localfs"' in new
            or "``localfs`` cache driver" in new
        ), (
            f"{rel}: replacement must cover cluster identity/readiness/recovery or per-master localfs Raft storage"
        )
        files.add(rel)
    # Raft identity, join sentinel + founder, ring ownership, and storage.
    assert "salt/cluster/consensus/storage.py" in files
    assert "salt/cluster/consensus/service.py" in files
    assert "salt/channel/server.py" in files
    assert "salt/master.py" in files
    assert "salt/cluster/ring_membership.py" in files

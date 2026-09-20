"""Cluster entrypoint tests: pre-seed the per-name master keypair.

Salt 3008 migrates legacy master.pem/pub to <id>.pem/pub on boot and
then DELETES the legacy files (MasterKeys._setup_keys -> cache.flush
on the master_keys bank). In production master.pem/pub are read-only
Secret mounts, so the delete raises SaltCacheError and the daemon
never starts. The entrypoint therefore pre-seeds writable
<NODE_NAME>.pem/pub copies (the stable pod DNS name, matching the
stamped `id`) so the migration is skipped. These tests run the real
preseed_master_keys function from cluster-entrypoint.sh against a
scratch keys dir.
"""

import os
import pathlib
import re
import stat
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "cluster-entrypoint.sh"

NODE = "salt-master-0.salt-master"


def _function(name):
    text = SCRIPT.read_text(encoding="utf-8")
    (body,) = re.findall(rf"^{name}\(\) \{{$.*?^}}$", text, re.MULTILINE | re.DOTALL)
    return body


def _run_preseed(keys_dir, node_name=NODE):
    driver = _function("log") + "\n" + _function("preseed_master_keys")
    driver += "\npreseed_master_keys\n"
    env = {
        **os.environ,
        "NODE_NAME": node_name,
        "KEYS_DIR": str(keys_dir),
        "KEY_OWNER": f"{os.getuid()}:{os.getgid()}",
        "LOG": str(keys_dir / "entrypoint.log"),
    }
    subprocess.run(["bash", "-c", driver], env=env, check=True)


def _seed_master(keys_dir):
    (keys_dir / "master.pem").write_text("PRIVATE-KEY-BYTES\n", encoding="utf-8")
    (keys_dir / "master.pub").write_text("PUBLIC-KEY-BYTES\n", encoding="utf-8")
    # Read-only like the Secret subPath mounts in production.
    os.chmod(keys_dir / "master.pem", 0o440)
    os.chmod(keys_dir / "master.pub", 0o440)


def test_preseed_creates_daemon_owned_name_keypair(tmp_path):
    _seed_master(tmp_path)
    _run_preseed(tmp_path)
    for ext, body in (("pem", "PRIVATE-KEY-BYTES\n"), ("pub", "PUBLIC-KEY-BYTES\n")):
        dest = tmp_path / f"{NODE}.{ext}"
        assert dest.read_text(encoding="utf-8") == body
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode == 0o400
    # The Secret-mounted originals are never touched.
    assert stat.S_IMODE((tmp_path / "master.pem").stat().st_mode) == 0o440


def test_preseed_idempotent_second_boot(tmp_path):
    _seed_master(tmp_path)
    _run_preseed(tmp_path)
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    _run_preseed(tmp_path)
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert before == after


def test_preseed_refreshes_on_secret_rotation(tmp_path):
    _seed_master(tmp_path)
    _run_preseed(tmp_path)
    # Rotation arrives as a fresh Secret mount; simulate the remount by
    # restoring write permission, replacing the content, and going
    # read-only again.
    for name, body in (
        ("master.pem", "ROTATED-PRIVATE\n"),
        ("master.pub", "ROTATED-PUBLIC\n"),
    ):
        target = tmp_path / name
        os.chmod(target, 0o600)
        target.write_text(body, encoding="utf-8")
        os.chmod(target, 0o440)
    _run_preseed(tmp_path)
    assert (tmp_path / f"{NODE}.pem").read_text(encoding="utf-8") == (
        "ROTATED-PRIVATE\n"
    )
    assert (tmp_path / f"{NODE}.pub").read_text(encoding="utf-8") == (
        "ROTATED-PUBLIC\n"
    )


def test_preseed_runs_synchronously_before_daemon_start():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    call = next(
        i for i, l in enumerate(lines) if re.match(r"\s*preseed_master_keys\s*$", l)
    )
    cluster = next(
        i for i, l in enumerate(lines) if re.match(r"\s*preseed_cluster_keys\s*$", l)
    )
    guard = next(
        i for i, l in enumerate(lines) if "POD_NAME:-" in l and "POD_IP:-" in l
    )
    entrypoint_exec = next(
        i for i, l in enumerate(lines) if l.startswith("exec /sbin/entrypoint.sh")
    )
    assert guard < call < cluster < entrypoint_exec


def _run_cluster_preseed(tmp_path, src_dir, pki_dir):
    driver = _function("log") + "\n" + _function("preseed_cluster_keys")
    driver += "\npreseed_cluster_keys\n"
    env = {
        **os.environ,
        "CLUSTER_KEYS_SRC": str(src_dir),
        "CLUSTER_PKI_DIR": str(pki_dir),
        "KEY_OWNER": f"{os.getuid()}:{os.getgid()}",
        "LOG": str(tmp_path / "entrypoint.log"),
    }
    subprocess.run(["bash", "-c", driver], env=env, check=True)


def test_preseed_cluster_keys_copies_secret_over_minted_pvc(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cluster.pem").write_text("PINNED-PEM\n", encoding="utf-8")
    (src / "cluster.pub").write_text("PINNED-PUB\n", encoding="utf-8")
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "cluster.pem").write_text("MINTED-PEM\n", encoding="utf-8")
    (pki / "cluster.pub").write_text("MINTED-PUB\n", encoding="utf-8")
    _run_cluster_preseed(tmp_path, src, pki)
    assert (pki / "cluster.pem").read_text(encoding="utf-8") == "PINNED-PEM\n"
    assert (pki / "cluster.pub").read_text(encoding="utf-8") == "PINNED-PUB\n"
    assert stat.S_IMODE((pki / "cluster.pem").stat().st_mode) == 0o400


def test_preseed_cluster_keys_skips_when_secret_absent(tmp_path):
    pki = tmp_path / "pki"
    pki.mkdir()
    (pki / "cluster.pem").write_text("KEEP\n", encoding="utf-8")
    _run_cluster_preseed(tmp_path, tmp_path / "no-src", pki)
    assert (pki / "cluster.pem").read_text(encoding="utf-8") == "KEEP\n"


MARKER = "# test identity marker"


def _shell_env(tmp_path, **extra):
    return {
        **os.environ,
        "MARKER": MARKER,
        "LOG": str(tmp_path / "entrypoint.log"),
        "SVC": "invalid.invalid",
        "BOOT_WAIT_SECS": "0",
        **extra,
    }


def _sh(names, call, env, check=True):
    driver = "\n".join(_function(name) for name in names) + f"\n{call}\n"
    return subprocess.run(
        ["bash", "-c", driver], env=env, check=check, capture_output=True, text=True
    )


PEER_A = "salt-master-0.salt-master"
PEER_B = "salt-master-1.salt-master"


PEER_C = "salt-master-2.salt-master"


def test_build_want_stamps_stable_names(tmp_path):
    # interface keeps the pod IP; id/cluster_node_id are self; peers
    # omit self (PEER_A == NODE).
    env = _shell_env(tmp_path)
    call = f'build_want "10.42.9.9" "{NODE}" "$(printf \'{PEER_A}\\n{PEER_B}\')"'
    proc = _sh(["build_want"], call, env)
    assert proc.stdout.splitlines() == [
        MARKER,
        "interface: 10.42.9.9",
        f"id: {NODE}",
        f"cluster_node_id: {NODE}",
        "cluster_peers:",
        f"  - {PEER_B}",
    ]


def test_build_want_strips_self_from_three(tmp_path):
    env = _shell_env(tmp_path)
    call = (
        f'build_want "10.42.9.9" "{NODE}" '
        f'"$(printf \'{PEER_A}\\n{PEER_B}\\n{PEER_C}\')"'
    )
    proc = _sh(["build_want"], call, env)
    lines = proc.stdout.splitlines()
    assert f"  - {PEER_B}" in lines
    assert f"  - {PEER_C}" in lines
    assert f"  - {PEER_A}" not in lines


def test_build_want_solo_keeps_self(tmp_path):
    env = _shell_env(tmp_path)
    call = f'build_want "10.42.9.9" "{NODE}" "{NODE}"'
    proc = _sh(["build_want"], call, env)
    assert proc.stdout.splitlines()[-2:] == ["cluster_peers:", f"  - {NODE}"]


def test_stamp_identity_roundtrip_replaces_never_duplicates(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("id: base\ninterface: 0.0.0.0\n", encoding="utf-8")
    env = {**_shell_env(tmp_path), "CONF": str(conf)}
    call = (
        f'want=$(build_want "10.42.9.9" "{NODE}" "$(printf \'{PEER_A}\')"); '
        'stamp_identity "$CONF" "$want"; stamped_have "$CONF"'
    )
    proc = _sh(["build_want", "stamped_have", "stamp_identity"], call, env)
    assert proc.stdout.splitlines() == [
        MARKER,
        "interface: 10.42.9.9",
        f"id: {NODE}",
        f"cluster_node_id: {NODE}",
        "cluster_peers:",
        f"  - {PEER_A}",
    ]
    text = conf.read_text(encoding="utf-8")
    assert text.count(MARKER) == 1
    assert "# superseded by cluster-entrypoint: id: base" in text
    assert "# superseded by cluster-entrypoint: interface: 0.0.0.0" in text
    # A changed peer set replaces the block instead of appending another.
    call2 = (
        f'want=$(build_want "10.42.9.9" "{NODE}" "$(printf \'{PEER_A}\\n{PEER_B}\')"); '
        'stamp_identity "$CONF" "$want"'
    )
    _sh(["build_want", "stamp_identity"], call2, env)
    text2 = conf.read_text(encoding="utf-8")
    assert text2.count(MARKER) == 1
    assert f"  - {PEER_B}" in text2
    assert f"  - {PEER_A}" not in text2.split("cluster_peers:")[-1]


def test_wait_for_peer_dns_proceeds_when_dns_empty(tmp_path):
    # SVC=invalid.invalid never resolves: with BOOT_WAIT_SECS=0 the
    # gate must not delay or fail the boot.
    proc = _sh(
        ["peers_live", "log", "wait_for_peer_dns"],
        "wait_for_peer_dns",
        _shell_env(tmp_path),
    )
    assert proc.returncode == 0
    assert "proceeding anyway" in (tmp_path / "entrypoint.log").read_text(
        encoding="utf-8"
    )


def test_wait_for_peer_dns_ready_when_dns_answers(tmp_path):
    # No getent on macOS, so stub name resolution in the driver: the
    # contract under test is the gate's ready branch, not getent itself.
    call = "getent() { printf '127.0.0.1 localhost\\n'; }\nwait_for_peer_dns"
    proc = _sh(["peers_live", "log", "wait_for_peer_dns"], call, _shell_env(tmp_path))
    assert proc.returncode == 0
    assert "peer DNS ready" in (tmp_path / "entrypoint.log").read_text(encoding="utf-8")


def test_dns_gate_runs_before_daemon_start():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    gate = next(
        i for i, l in enumerate(lines) if re.match(r"\s*wait_for_peer_dns\s*$", l)
    )
    entrypoint_exec = next(
        i for i, l in enumerate(lines) if l.startswith("exec /sbin/entrypoint.sh")
    )
    assert gate < entrypoint_exec


def _peers_dir(tmp_path, *names):
    peers = tmp_path / "peers"
    peers.mkdir()
    for name in names:
        (peers / name).write_text("PUB\n", encoding="utf-8")
    return peers


def test_api_peers_falls_back_without_token(tmp_path):
    env = {**_shell_env(tmp_path), "SA_TOKEN_FILE": str(tmp_path / "no-token")}
    proc = _sh(["api_query", "api_peers"], "api_peers", env, check=False)
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_prune_dead_keys_keeps_live_names_and_master(tmp_path):
    peers = _peers_dir(
        tmp_path,
        f"{PEER_A}.pub",
        "salt-master-9.salt-master.pub",
        "master.pub",
        "notes.txt",
    )
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    env = _shell_env(tmp_path)
    _sh(["log", "prune_dead_keys"], f'prune_dead_keys "{peers}" "{PEER_A}"', env)
    remaining = sorted(p.name for p in peers.iterdir())
    assert remaining == ["master.pub", "notes.txt", f"{PEER_A}.pub"]
    assert "pruned stale peer key salt-master-9.salt-master" in (
        tmp_path / "entrypoint.log"
    ).read_text(encoding="utf-8")


def test_peers_fallback_constructs_ordinal_names(tmp_path):
    # No Kubernetes API here: the fallback builds stable names from
    # the pod-name prefix, never IPs (a mixed set matches nothing).
    env = {
        **_shell_env(tmp_path),
        "POD_NAME": "salt-master-1",
        "PEER_REPLICAS": "3",
        "MASTER_HEADLESS_SERVICE": "salt-master",
    }
    proc = _sh(["peers_fallback"], "peers_fallback", env)
    assert proc.stdout.splitlines() == [
        "salt-master-0.salt-master",
        "salt-master-1.salt-master",
        "salt-master-2.salt-master",
    ]


def test_prune_dead_keys_missing_dir_is_fine(tmp_path):
    proc = _sh(
        ["log", "prune_dead_keys"],
        f'prune_dead_keys "{tmp_path}/no-such-dir" "10.0.0.1"',
        _shell_env(tmp_path),
    )
    assert proc.returncode == 0


def _recover_env(tmp_path, **extra):
    return {
        **_shell_env(tmp_path),
        "RECOVER_AFTER_N": "2",
        "RECOVER_MIN_SECS": "0",
        **extra,
    }


def _recover_call(peers, peers_dir, state_dir, self_ip, n):
    return (
        f'peer_recovery_action "{peers}" "{peers_dir}" "{state_dir}" "{self_ip}" "{n}"'
    )


GHOST = "salt-master-9.salt-master"


def test_recovery_full_set_marks_joined(tmp_path):
    peers = _peers_dir(tmp_path, f"{PEER_A}.pub", f"{PEER_B}.pub")
    state = tmp_path / "state"
    state.mkdir()
    proc = _sh(
        ["log", "peer_recovery_action"],
        _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "0"),
        _recover_env(tmp_path),
    )
    assert proc.stdout.strip() == "ok"
    assert (state / ".joined").exists()


def test_recovery_fresh_identity_waits_and_resets(tmp_path):
    peers = _peers_dir(tmp_path, f"{PEER_B}.pub")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".joined").write_text("old\n", encoding="utf-8")
    proc = _sh(
        ["log", "peer_recovery_action"],
        _recover_call(f"{GHOST}\n{PEER_B}", peers, state, GHOST, "9"),
        _recover_env(tmp_path),
    )
    assert proc.stdout.strip() == "wait"
    assert not (state / ".joined").exists()


def test_recovery_bounces_stalled_join_with_rate_limit(tmp_path):
    peers = _peers_dir(tmp_path, f"{PEER_A}.pub")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".joined").write_text("old\n", encoding="utf-8")
    names = ["log", "peer_recovery_action"]
    call = _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "0")
    assert _sh(names, call, _recover_env(tmp_path)).stdout.strip() == "wait"
    call = _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "1")
    assert _sh(names, call, _recover_env(tmp_path)).stdout.strip() == "wait"
    call = _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "2")
    assert _sh(names, call, _recover_env(tmp_path)).stdout.strip() == "bounce"
    assert (state / ".last-recover-bounce").exists()
    # A second streak right after the bounce waits: rate-limited.
    limited = _recover_env(tmp_path, RECOVER_MIN_SECS="3600")
    call = _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "9")
    assert _sh(names, call, limited).stdout.strip() == "wait"


def test_recovery_never_joined_means_wait_not_bounce(tmp_path):
    peers = _peers_dir(tmp_path, f"{PEER_A}.pub")
    state = tmp_path / "state"
    state.mkdir()
    proc = _sh(
        ["log", "peer_recovery_action"],
        _recover_call(f"{PEER_A}\n{PEER_B}", peers, state, PEER_A, "9"),
        _recover_env(tmp_path),
    )
    assert proc.stdout.strip() == "wait"


def _maiden_env(tmp_path, running):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = "RUNNING" if running else "STOPPED"
    (bindir / "supervisorctl").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{tmp_path}/supervisor-calls"\n'
        'if [ "$1" = "status" ]; then\n'
        f'  echo "salt-master {state}   pid 999, uptime 0:00:01"\n'
        "fi\n",
        encoding="utf-8",
    )
    os.chmod(bindir / "supervisorctl", 0o755)
    return {**_shell_env(tmp_path), "PATH": f"{bindir}:{os.environ['PATH']}"}


def _maiden_call(conf, bounced, node=NODE):
    return (
        f'want=$(build_want "10.42.9.9" "{node}" "{PEER_A}"); '
        f'apply_maiden_stamp "{conf}" "$want" "{bounced}"'
    )


_MAIDEN_FUNCS = [
    "log",
    "build_want",
    "stamp_identity",
    "daemon_running",
    "apply_maiden_stamp",
]


def test_maiden_stamp_bounces_running_daemon_once(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("id: base\ninterface: 0.0.0.0\n", encoding="utf-8")
    proc = _sh(_MAIDEN_FUNCS, _maiden_call(conf, "0"), _maiden_env(tmp_path, True))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1"
    stamped = conf.read_text(encoding="utf-8")
    assert f"id: {NODE}" in stamped
    assert f"  - {PEER_A}" in stamped
    calls = (tmp_path / "supervisor-calls").read_text(encoding="utf-8")
    assert "restart salt-master" in calls


def test_maiden_stamp_skips_bounce_when_daemon_down(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("id: base\ninterface: 0.0.0.0\n", encoding="utf-8")
    proc = _sh(_MAIDEN_FUNCS, _maiden_call(conf, "0"), _maiden_env(tmp_path, False))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "0"
    # Stamped anyway: a daemon starting later reads the identity.
    assert f"id: {NODE}" in conf.read_text(encoding="utf-8")
    assert "restart" not in (tmp_path / "supervisor-calls").read_text(
        encoding="utf-8"
    )


def test_maiden_stamp_never_bounces_twice(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("id: base\ninterface: 0.0.0.0\n", encoding="utf-8")
    proc = _sh(_MAIDEN_FUNCS, _maiden_call(conf, "1"), _maiden_env(tmp_path, True))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1"
    calls = tmp_path / "supervisor-calls"
    assert not calls.exists() or "restart" not in calls.read_text(encoding="utf-8")


def test_wrapper_drops_stale_joined_before_watcher_start():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    drop = next(
        i
        for i, l in enumerate(lines)
        if 'rm -f "$KEYS_DIR/.joined" "$READY_MARK"' in l
        or 'rm -f "$KEYS_DIR/.joined"' in l
    )
    ident = next(
        i for i, l in enumerate(lines) if 'write_identity "$IDENTITY_CONF"' in l
    )
    cache = next(i for i, l in enumerate(lines) if 'mkdir -p "$CACHE_DIR"' in l)
    entrypoint_exec = next(
        i for i, l in enumerate(lines) if l.startswith("exec /sbin/entrypoint.sh")
    )
    assert drop < ident < entrypoint_exec
    assert cache < entrypoint_exec
    assert any("health/ready" in l for l in lines)


def _fallback_env(tmp_path):
    return {
        # Daemon down: phase-1 must stamp without bouncing.
        **_maiden_env(tmp_path, False),
        "POD_NAME": "salt-master-0",
        "PEER_REPLICAS": "3",
        "MASTER_HEADLESS_SERVICE": "salt-master",
    }


_FALLBACK_FUNCS = [
    "log",
    "peers_fallback",
    "build_want",
    "stamped_have",
    "stamp_identity",
    "daemon_running",
    "apply_maiden_stamp",
    "maiden_stamp_fallback",
]


def test_phase1_stamps_constructed_names_without_api(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("cluster_id: abc\n", encoding="utf-8")
    proc = _sh(
        _FALLBACK_FUNCS,
        f'maiden_stamp_fallback "{conf}" "10.42.9.9" "{NODE}" "0"',
        _fallback_env(tmp_path),
    )
    assert proc.returncode == 0
    stamped = conf.read_text(encoding="utf-8")
    # Constructed set minus self: preflight is non-empty, no loopback vote.
    assert f"id: {NODE}" in stamped
    assert "  - salt-master-1.salt-master" in stamped
    assert "  - salt-master-2.salt-master" in stamped
    assert "  - salt-master-0.salt-master" not in stamped.split("cluster_peers:")[-1]


def test_write_identity_swaps_atomically(tmp_path):
    dest = tmp_path / "cluster-identity.conf"
    env = {
        **_shell_env(tmp_path),
        "KEY_OWNER": f"{os.getuid()}:{os.getgid()}",
    }
    proc = _sh(
        ["build_want", "write_identity"],
        f'want=$(build_want "10.42.9.9" "{NODE}" "$(printf \'{PEER_A}\\n{PEER_B}\')"); '
        f'write_identity "{dest}" "$want"',
        env,
    )
    assert proc.returncode == 0
    text = dest.read_text(encoding="utf-8")
    assert f"id: {NODE}" in text
    assert f"  - {PEER_B}" in text
    assert f"  - {PEER_A}" not in text.split("cluster_peers:")[-1]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("cluster-identity.conf.")]


def test_stamp_identity_swaps_atomically_and_preserves_mode(tmp_path):
    conf = tmp_path / "master"
    conf.write_text("id: base\ninterface: 0.0.0.0\n", encoding="utf-8")
    os.chmod(conf, 0o640)
    before_inode = conf.stat().st_ino
    proc = _sh(
        ["build_want", "stamp_identity"],
        f'want=$(build_want "10.42.9.9" "{NODE}" "{PEER_A}"); '
        'stamp_identity "$CONF" "$want"',
        {**_shell_env(tmp_path), "CONF": str(conf)},
    )
    assert proc.returncode == 0
    # Rename-swap: a daemon reading mid-stamp sees the old or the new
    # file, never a half-written one (empty cluster_peers preflight).
    assert conf.stat().st_ino != before_inode
    if sys.platform.startswith("linux"):
        assert stat.S_IMODE(conf.stat().st_mode) == 0o640
    assert f"id: {NODE}" in conf.read_text(encoding="utf-8")
    # No temp turds beside the config.
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("master.")]


def test_identity_drop_in_written_before_exec_and_recovery_is_gated():
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    ident = next(
        i
        for i, l in enumerate(lines)
        if 'write_identity "$IDENTITY_CONF"' in l
    )
    entrypoint_exec = next(
        i for i, l in enumerate(lines) if l.startswith("exec /sbin/entrypoint.sh")
    )
    assert ident < entrypoint_exec
    assert "while ! supervisorctl status" not in text
    boot = text.split('if [ -n "${POD_NAME:-}"', 1)[1].split(
        "exec /sbin/entrypoint.sh", 1
    )[0]
    assert "apply_maiden_stamp" not in boot
    assert boot.count("supervisorctl restart salt-master") == 1
    assert "peers changed" not in text
    # Recovery bounce sits inside the complete-replica-view guard.
    prune_at = next(
        i for i, l in enumerate(lines) if "prune_dead_keys" in l and "PEER_KEYS" in l
    )
    recover_at = next(
        i for i, l in enumerate(lines) if "peer_recovery_action" in l and "KEYS_DIR" in l
    )
    assert prune_at < recover_at

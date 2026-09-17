"""Cluster entrypoint tests: pre-seed the per-IP master keypair.

Salt 3008 migrates legacy master.pem/pub to <id>.pem/pub on boot and
then DELETES the legacy files (MasterKeys._setup_keys -> cache.flush
on the master_keys bank). In production master.pem/pub are read-only
Secret mounts, so the delete raises SaltCacheError and the daemon
never starts. The entrypoint therefore pre-seeds writable
<POD_IP>.pem/pub copies so the migration is skipped. These tests run
the real preseed_master_keys function from cluster-entrypoint.sh
against a scratch keys dir.
"""

import os
import pathlib
import re
import stat
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "cluster-entrypoint.sh"


def _function(name):
    text = SCRIPT.read_text(encoding="utf-8")
    (body,) = re.findall(rf"^{name}\(\) \{{$.*?^}}$", text, re.MULTILINE | re.DOTALL)
    return body


def _run_preseed(keys_dir, pod_ip="10.42.9.9"):
    driver = _function("log") + "\n" + _function("preseed_master_keys")
    driver += "\npreseed_master_keys\n"
    env = {
        **os.environ,
        "POD_IP": pod_ip,
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


def test_preseed_creates_daemon_owned_ip_keypair(tmp_path):
    _seed_master(tmp_path)
    _run_preseed(tmp_path)
    for ext, body in (("pem", "PRIVATE-KEY-BYTES\n"), ("pub", "PUBLIC-KEY-BYTES\n")):
        dest = tmp_path / f"10.42.9.9.{ext}"
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
    assert (tmp_path / "10.42.9.9.pem").read_text(encoding="utf-8") == (
        "ROTATED-PRIVATE\n"
    )
    assert (tmp_path / "10.42.9.9.pub").read_text(encoding="utf-8") == (
        "ROTATED-PUBLIC\n"
    )


def test_preseed_runs_synchronously_before_daemon_start():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    call = next(
        i for i, l in enumerate(lines) if re.match(r"\s*preseed_master_keys\s*$", l)
    )
    guard = next(
        i for i, l in enumerate(lines) if "POD_NAME:-" in l and "POD_IP:-" in l
    )
    entrypoint_exec = next(
        i for i, l in enumerate(lines) if l.startswith("exec /sbin/entrypoint.sh")
    )
    assert guard < call < entrypoint_exec

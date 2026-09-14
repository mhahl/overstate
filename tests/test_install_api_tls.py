"""install-api-tls.sh polls salt-api back instead of single-shot curl.

CherryPy is not listening yet a few seconds after `supervisorctl
restart`; a lone curl exits 7 and used to fail the whole oneshot unit
under `set -e`. The script must keep polling until 401 or attempts run
out. Fakes on PATH stand in for podman/curl.
"""

import os
import stat
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _run_script(tmp_path, ok_code):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "curl.calls"
    _stub(
        str(bindir / "podman"),
        "#!/bin/sh\nexit 0\n",
    )
    _stub(
        str(bindir / "curl"),
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f"echo $((n + 1)) > {counter}\n"
        'if [ "$n" -lt 2 ]; then printf "%s" "000"; exit 7; fi\n'
        f'printf "%s" "{ok_code}"\n',
    )
    tls = tmp_path / "tls"
    tls.mkdir()
    for name in ("api.crt", "api.key", "ca.crt"):
        (tls / name).write_text("x")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    proc = subprocess.run(
        [
            "bash",
            "scripts/install-api-tls.sh",
            str(tls / "api.crt"),
            str(tls / "api.key"),
            "salt-master",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc, counter


def test_api_tls_waits_for_restarted_api(tmp_path):
    proc, counter = _run_script(tmp_path, "401")
    assert proc.returncode == 0, proc.stderr
    assert "401" in proc.stdout
    assert int(counter.read_text()) >= 3  # polled, not single-shot


def test_api_tls_accepts_200_from_login(tmp_path):
    # Some salt-api versions answer the anonymous /login check with 200.
    proc, _ = _run_script(tmp_path, "200")
    assert proc.returncode == 0, proc.stderr
    assert "200" in proc.stdout

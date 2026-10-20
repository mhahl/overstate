"""Pillar source browser: browse the pillar/ tree, view any file, edit
only non-secret files, and refuse edits under secrets/ (the Encrypted
pillar editor owns writes there)."""

import subprocess

import pytest

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "pw"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture()
def pf_env(tmp_path, monkeypatch):
    """Checkout with a pillar tree: top.sls, a plaintext common.sls, and
    an encrypted-looking secrets/netbird.sls (NACL envelope literal)."""
    work = tmp_path / "work"
    salt = work / "salt"
    salt.mkdir(parents=True)
    (salt / "top.sls").write_text("base:\n  '*': []\n")
    pillar = work / "pillar"
    (pillar / "secrets").mkdir(parents=True)
    (pillar / "top.sls").write_text("base:\n  '*':\n    - common\n    - secrets.netbird\n")
    (pillar / "common.sls").write_text("ntp:\n  servers:\n    - pool.ntp.org\n")
    (pillar / "secrets" / "netbird.sls").write_text(
        "netbird:\n  setup_key: NACL[fake-ciphertext]\n"
    )
    _git(work, "init", "-qb", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "seed")
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(salt)
    # The pillar browser refreshes the master fileserver after a save; a
    # stub keeps tests hermetic.
    import overstate_ui.pillar_files as pf

    monkeypatch.setattr(pf, "_refresh_fileserver", lambda: None)
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add(User(username="op", role="operator", password_hash=_ph.hash(PW)))
        session.add(User(username="vie", role="viewer", password_hash=_ph.hash(PW)))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": PW})
    c.app = app
    c.work = work
    return c


def _login(c, username):
    c.post("/logout")
    c.post("/login", data={"username": username, "password": PW})


def test_index_lists_toplevel_and_secrets(pf_env):
    html = pf_env.get("/pillar/files/").data.decode()
    assert "top.sls" in html
    assert "common.sls" in html
    assert "secrets/" in html
    assert "netbird.sls" in html


def test_view_shows_yaml_and_marks_secret(pf_env):
    html = pf_env.get("/pillar/files/view?path=common.sls").data.decode()
    assert "pool.ntp.org" in html
    assert "Encrypted pillar editor" not in html
    secret = pf_env.get("/pillar/files/view?path=secrets/netbird.sls").data.decode()
    assert "NACL[fake-ciphertext]" in secret
    assert "encrypted" in secret


def test_edit_secret_refuses(pf_env):
    rv = pf_env.get("/pillar/files/edit?path=secrets/netbird.sls", follow_redirects=True)
    assert "Encrypted pillar editor" in rv.data.decode()
    # Nothing changed on disk.
    text = (pf_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "NACL[fake-ciphertext]" in text


def test_edit_and_save_plaintext_commits(pf_env):
    import re

    html = pf_env.get("/pillar/files/edit?path=common.sls").data.decode()
    assert "editor-mount" in html
    m = re.search(r'name="base_sha" value="([^"]+)"', html)
    base_sha = m.group(1) if m else "nogit"
    m2 = re.search(r'name="base_hash" value="([^"]+)"', html)
    base_hash = m2.group(1) if m2 else ""
    rv = pf_env.post(
        "/pillar/files/save",
        data={
            "path": "common.sls",
            "content": "ntp:\n  servers:\n    - 0.pool.ntp.org\n    - 1.pool.ntp.org\n",
            "base_sha": base_sha,
            "base_hash": base_hash,
        },
        follow_redirects=True,
    )
    assert "Saved common.sls" in rv.data.decode()
    text = (pf_env.work / "pillar" / "common.sls").read_text()
    assert "0.pool.ntp.org" in text
    proc = _git(pf_env.work, "status", "--porcelain")
    assert proc.stdout.strip() == ""


def test_save_secret_refuses(pf_env):
    rv = pf_env.post(
        "/pillar/files/save",
        data={"path": "secrets/netbird.sls", "content": "netbird:\n  setup_key: PLAINTEXT!\n"},
        follow_redirects=True,
    )
    assert "read-only in the file browser" in rv.data.decode()
    text = (pf_env.work / "pillar" / "secrets" / "netbird.sls").read_text()
    assert "PLAINTEXT!" not in text


def test_viewer_reads_but_cannot_edit(pf_env):
    _login(pf_env, "vie")
    html = pf_env.get("/pillar/files/").data.decode()
    assert "common.sls" in html
    rv = pf_env.post(
        "/pillar/files/save",
        data={"path": "common.sls", "content": "x: 1\n"},
        follow_redirects=True,
    )
    assert rv.status_code == 403


def test_traversal_refused(pf_env):
    assert pf_env.get("/pillar/files/view?path=../salt/top.sls").status_code == 404
    assert pf_env.get("/pillar/files/view?path=%2e%2e%2fsalt/top.sls").status_code in (400, 404)
"""Tracked formulas: table reconcile, vendor/update/remove routes, RBAC, audit."""

import shutil
import subprocess

import pytest

import overstate_ui.formulas as formulas_mod
import overstate_ui.git_sync as gs
from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, Formula, User

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required"
)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_checkout(roots):
    roots.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=roots)
    _git("config", "user.email", "t@t", cwd=roots)
    _git("config", "user.name", "t", cwd=roots)
    (roots / "top.sls").write_text("base:\n  '*':\n    - demo\n")
    _git("add", "-A", cwd=roots)
    _git("commit", "-qm", "seed", cwd=roots)


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    roots = tmp_path / "srv" / "salt"
    _init_checkout(roots)
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(roots)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(
            User(
                username="op",
                password_hash=authmod._ph.hash("pw"),
                role="operator",
            )
        )
        session.add(
            User(
                username="vie",
                password_hash=authmod._ph.hash("pw"),
                role="viewer",
            )
        )
        session.commit()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    monkeypatch.setattr(formulas_mod, "_refresh_fileserver", lambda: None)
    return app, client, roots


def _login_as(client, username):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": "pw"})


def _actions(app):
    with app.app_context():
        return [e.action for e in get_session().query(AuditEvent).all()]


def test_validate_formula_name():
    assert gs.validate_formula_name("nginx")["ok"]
    assert gs.validate_formula_name("my-formula_2")["ok"]
    assert not gs.validate_formula_name("../escape")["ok"]
    assert not gs.validate_formula_name("Top")["ok"]
    assert not gs.validate_formula_name("")["ok"]
    assert not gs.validate_formula_name("_modules")["ok"]


def test_formulas_page_renders(web_client):
    _, client, _roots = web_client
    rv = client.get("/formulas/")
    assert rv.status_code == 200
    assert b"Formulas" in rv.data
    assert b"Add formula" in rv.data


def test_files_page_links_to_formulas_and_state_repo(web_client):
    _, client, _roots = web_client
    html = client.get("/files/").data.decode()
    assert 'id="file-update-actions"' in html
    assert 'aria-label="Checkout status and sync"' in html
    assert 'href="/formulas/"' in html
    assert 'href="/settings/repo/"' in html
    assert 'href="/files/?tab=files"' not in html


def test_formulas_subtabs_split_tracked_and_untracked(web_client):
    app, client, roots = web_client
    _login_as(client, "op")
    (roots / "redis").mkdir()
    (roots / "redis" / "init.sls").write_text("# redis\n")
    with app.app_context():
        get_session().add(
            Formula(name="nginx", url="https://git.example.com/n.git", branch=None)
        )
        get_session().commit()
    tracked = client.get("/formulas/", query_string={"fsub": "tracked"}).data.decode()
    assert "nginx/" in tracked
    assert "redis/" not in tracked
    untracked = client.get("/formulas/", query_string={"fsub": "untracked"}).data.decode()
    assert "redis/" in untracked
    assert 'aria-label="Formula views"' in untracked


def test_formulas_viewer_cannot_add(web_client):
    _, client, _roots = web_client
    _login_as(client, "vie")
    rv = client.post(
        "/formulas/add",
        data={"name": "nginx", "url": "https://git.example.com/n.git"},
    )
    assert rv.status_code == 403


def test_formulas_operator_can_add_with_stub(web_client, monkeypatch):
    app, client, roots = web_client
    _login_as(client, "op")

    def fake_vendor(name, url, branch, auth, token):
        (roots / name).mkdir(parents=True)
        (roots / name / "init.sls").write_text("# stub\n")
        return {"ok": True, "name": name, "sha": "abc123def456"}

    monkeypatch.setattr(formulas_mod, "vendor_formula", fake_vendor)
    rv = client.post(
        "/formulas/add",
        data={
            "name": "nginx",
            "url": "https://git.example.com/n.git",
            "branch": "",
            "auth": "shared",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"Vendored formula" in rv.data
    with app.app_context():
        row = get_session().query(Formula).filter_by(name="nginx").first()
        assert row is not None and row.url == "https://git.example.com/n.git"
    assert "formula-add:nginx:abc123def456" in _actions(app)


def test_formulas_add_refuses_bad_url(web_client):
    app, client, _roots = web_client
    _login_as(client, "op")
    rv = client.post(
        "/formulas/add",
        data={"name": "nginx", "url": "file:///etc/passwd"},
        follow_redirects=True,
    )
    assert b"Add refused" in rv.data
    with app.app_context():
        assert get_session().query(Formula).filter_by(name="nginx").first() is None


def test_formulas_add_never_echoes_token(web_client, monkeypatch):
    _, client, _roots = web_client
    _login_as(client, "op")
    seen = {}

    def fake_vendor(name, url, branch, auth, token):
        seen["token"] = token
        seen["auth"] = auth
        return {"ok": True, "name": name, "sha": "abc"}

    monkeypatch.setattr(formulas_mod, "vendor_formula", fake_vendor)
    rv = client.post(
        "/formulas/add",
        data={
            "name": "nginx",
            "url": "https://git.example.com/n.git",
            "auth": "token",
            "token": "s3cr3t-token-value",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"s3cr3t-token-value" not in rv.data
    assert seen == {"token": "s3cr3t-token-value", "auth": "token"}


def test_formulas_track_untracked_dir(web_client):
    app, client, roots = web_client
    _login_as(client, "op")
    (roots / "redis").mkdir()
    (roots / "redis" / "init.sls").write_text("# redis\n")
    rv = client.get("/formulas/", query_string={"fsub": "untracked"})
    assert b"redis/" in rv.data
    rv = client.post(
        "/formulas/track",
        data={"name": "redis", "url": "https://git.example.com/r.git"},
        follow_redirects=True,
    )
    assert b"Tracking" in rv.data
    with app.app_context():
        assert get_session().query(Formula).filter_by(name="redis").first() is not None
    assert "formula-track:redis" in _actions(app)


def test_formulas_update_with_stub(web_client, monkeypatch):
    app, client, _roots = web_client
    _login_as(client, "op")
    with app.app_context():
        get_session().add(
            Formula(name="nginx", url="https://git.example.com/n.git", branch=None)
        )
        get_session().commit()
    monkeypatch.setattr(
        formulas_mod,
        "update_formula",
        lambda *a, **k: {"ok": True, "name": "nginx", "sha": "deedbeef1234"},
    )
    rv = client.post(
        "/formulas/update/nginx",
        data={"auth": "shared"},
        follow_redirects=True,
    )
    assert b"Updated formula" in rv.data
    assert "formula-update:nginx:deedbeef1234" in _actions(app)


def test_formulas_remove_preview_then_confirm(web_client):
    app, client, roots = web_client
    _login_as(client, "op")
    with app.app_context():
        get_session().add(
            Formula(name="nginx", url="https://git.example.com/n.git", branch=None)
        )
        get_session().commit()
    (roots / "nginx").mkdir()
    (roots / "nginx" / "init.sls").write_text("# nginx\n")
    _git("add", "-A", cwd=roots)
    _git(
        "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "vendor",
        cwd=roots,
    )
    preview = client.post("/formulas/remove/nginx")
    assert preview.status_code == 200
    assert b"Remove" in preview.data and b"init.sls" in preview.data
    assert (roots / "nginx").is_dir()  # preview destroys nothing
    done = client.post(
        "/formulas/remove/nginx",
        data={"confirm": "1"},
        follow_redirects=True,
    )
    assert b"Removed formula" in done.data
    assert not (roots / "nginx").exists()  # files deleted per decision 3
    with app.app_context():
        assert get_session().query(Formula).filter_by(name="nginx").first() is None
    assert "formula-remove:nginx" in _actions(app)


@needs_git
def test_formula_committed_detects_git_origin(tmp_path):
    from flask import Flask

    roots = tmp_path / "srv" / "salt"
    _init_checkout(roots)
    app = Flask(__name__)
    app.config["FILE_ROOTS"] = str(roots)
    with app.app_context():
        assert gs.formula_committed("missing") is False
        (roots / "committed").mkdir()
        (roots / "committed" / "init.sls").write_text("# c\n")
        assert gs.formula_committed("committed") is False  # not yet added
        _git("add", "-A", cwd=roots)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "f", cwd=roots)
        assert gs.formula_committed("committed") is True


def test_untracked_tab_labels_git_origin(web_client):
    _app, client, roots = web_client
    _login_as(client, "op")
    # committed formula: lands via git, no DB row
    (roots / "fromrepo").mkdir()
    (roots / "fromrepo" / "init.sls").write_text("# f\n")
    _git("add", "-A", cwd=roots)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fromrepo", cwd=roots)
    # stray dir: on disk but never committed
    (roots / "stray").mkdir()
    (roots / "stray" / "init.sls").write_text("# s\n")
    html = client.get("/formulas/", query_string={"fsub": "untracked"}).data.decode()
    assert "from states repo" in html
    assert "fromrepo/" in html
    assert "not committed yet" in html
    assert "stray/" in html
    # in-repo formula: URL/ref/track disabled; stray dir: enabled
    assert 'aria-label="Source URL for fromrepo" required disabled' in html
    assert '>Track</button>' in html.replace(' disabled title="Source is the states repo; tracking is not needed.">Track</button>', ">Track</button>")
    assert 'aria-label="Source URL for stray" required>' in html


def test_track_refuses_from_repo_formula(web_client):
    app, client, roots = web_client
    _login_as(client, "op")
    (roots / "fromrepo").mkdir()
    (roots / "fromrepo" / "init.sls").write_text("# f\n")
    _git("add", "-A", cwd=roots)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fromrepo", cwd=roots)
    rv = client.post(
        "/formulas/track",
        data={"name": "fromrepo", "url": "https://git.example.com/f.git"},
        follow_redirects=True,
    )
    assert b"comes from the states repo" in rv.data
    with app.app_context():
        assert get_session().query(Formula).filter_by(name="fromrepo").first() is None
    assert "formula-track-refused:fromrepo:from-repo" in _actions(app)


def test_update_formula_replaces_tree(web_client, monkeypatch):
    """Regression: update must replace the installed tree, not fail on a
    pre-created dest dir (the old code mkdir'ed dest, then
    _move_formula_contents refused because it already existed)."""
    app, client, roots = web_client
    _login_as(client, "op")
    (roots / "nginx").mkdir()
    (roots / "nginx" / "init.sls").write_text("# old\n")
    _git("add", "-A", cwd=roots)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "vendor", cwd=roots)

    def fake_clone(url, branch, auth, token):
        tmp = roots.parent / (roots.name + ".__formula_tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        (tmp / "init.sls").write_text("# updated\n")
        (tmp / "config.sls").write_text("# new file\n")
        return {"ok": True, "tmp": tmp, "sha": "c0ffee1234567890"}

    monkeypatch.setattr(gs, "_clone_formula_source", fake_clone)
    with app.app_context():
        result = gs.update_formula("nginx", "https://git.example.com/n.git", None, "shared", None)
    assert result["ok"], result
    assert (roots / "nginx" / "init.sls").read_text() == "# updated\n"
    assert (roots / "nginx" / "config.sls").is_file()
    head = subprocess.run(
        ["git", "-C", str(roots), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head


@needs_git
def test_vendor_and_remove_files_roundtrip(tmp_path):
    """Real git: clone a local formula repo, install, commit, remove."""
    from flask import Flask

    formula_repo = tmp_path / "upstream"
    formula_repo.mkdir()
    _git("init", "-q", cwd=formula_repo)
    _git("config", "user.email", "t@t", cwd=formula_repo)
    _git("config", "user.name", "t", cwd=formula_repo)
    (formula_repo / "init.sls").write_text("# formula\n")
    _git("add", "-A", cwd=formula_repo)
    _git("commit", "-qm", "formula v1", cwd=formula_repo)

    roots = tmp_path / "srv" / "salt"
    _init_checkout(roots)
    app = Flask(__name__)
    app.config["FILE_ROOTS"] = str(roots)
    with app.app_context():
        result = gs._clone_formula_source(f"file://{formula_repo}", None, "shared", None)
        assert result["ok"], result
        installed = gs._install_formula_tree(result["tmp"], "nginx")
        assert installed["ok"], installed
        assert (roots / "nginx" / "init.sls").is_file()
        _git("add", "-A", cwd=roots)
        _git(
            "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "vendor",
            cwd=roots,
        )
        removed = gs.remove_formula_files("nginx")
        assert removed["ok"], removed
        assert not (roots / "nginx").exists()

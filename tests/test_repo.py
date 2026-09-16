"""Repo tab: clone a missing checkout, repoint origin, reset to remote.

Reads real git behavior from local bare repos (file:// transport is a
test-only path straight into _clone, never through validation); the URL
allowlist itself is unit-tested against hostile inputs. argv capture
proves user input never reaches a flag position.
"""

import shutil
import subprocess

import pytest

import overstate_ui.files as files_mod
import overstate_ui.git_sync as gs
from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, User

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required"
)


def _bare(tmp_path, name="origin.git"):
    dest = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def _seed(bare, tmp_path, files=("top.sls",)):
    work = tmp_path / "seedwork"
    subprocess.run(
        ["git", "clone", str(bare), str(work)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    for name in files:
        (work / name).write_text("# seed\n")
        subprocess.run(["git", "-C", str(work), "add", name], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True
    )


@pytest.fixture()
def ctx(tmp_path):
    roots = tmp_path / "srv" / "salt"
    app = create_app(TestConfig)
    app.config["FILE_ROOTS"] = str(roots)
    return app.app_context(), roots


def test_validate_allows_https_and_ssh():
    assert gs.validate_repo_url("https://git.example.com/salt/states.git")["ok"]
    assert gs.validate_repo_url("git@git.example.com:salt/states.git")["ok"]
    assert gs.validate_repo_url("ssh://git@git.example.com/srv/states.git")["ok"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "ext::sh -c id",
        "https://tok@git.example.com/r.git",  # creds belong in the token field
        "--upload-pack=id",
        "https://git.example.com/a b.git",
        "git@evil com:x.git",
        "https://",
    ],
)
def test_validate_rejects_hostile_urls(bad):
    assert not gs.validate_repo_url(bad)["ok"]


@pytest.mark.parametrize("bad", ["--x", "-b", "../escape", "a b", "/lead"])
def test_validate_rejects_hostile_branches(bad):
    assert not gs.validate_branch(bad)["ok"]


def test_validate_branch_blank_means_head():
    assert gs.validate_branch("") == {"ok": True, "branch": None}


@needs_git
def test_clone_bootstraps_missing_checkout(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        result = gs._clone(f"file://{bare}", None)
    assert result["ok"], result
    assert (roots / "top.sls").is_file()
    assert (roots / ".git").is_dir()


@needs_git
def test_clone_refuses_into_existing_checkout(ctx, tmp_path):
    app_ctx, _roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        again = gs._clone(f"file://{bare}", None)
    assert not again["ok"]


@needs_git
def test_clone_argv_never_carries_flags(ctx, tmp_path, monkeypatch):
    app_ctx, _roots = ctx
    seen = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real_run(["git", "version"], capture_output=True, text=True)

    monkeypatch.setattr(subprocess, "run", spy)
    with app_ctx:
        gs._clone("file:///x.git", "--upload-pack=id")
    # Refused or sanitized — either way the hostile string reaches no argv.
    assert all("--upload-pack=id" != arg for argv in seen for arg in argv[1:])


@needs_git
def test_reset_hard_discards_tracked_edits(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        (roots / "top.sls").write_text("# vandalized\n")
        preview = gs.reset_preview()
        assert preview["ok"] and "top.sls" in preview["dirty"]
        result = gs.reset_hard(clean_untracked=False)
    assert result["ok"], result
    assert (roots / "top.sls").read_text() == "# seed\n"


@needs_git
def test_reset_refuses_when_diverged(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        (roots / "local.sls").write_text("# local commit\n")
        subprocess.run(["git", "-C", str(roots), "add", "local.sls"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(roots),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "local",
            ],
            check=True,
            capture_output=True,
        )
        result = gs.reset_hard(clean_untracked=False)
    assert not result["ok"] and "unpushed" in result["reason"]
    assert (roots / "local.sls").is_file()  # nothing was destroyed


@needs_git
def test_reclone_replaces_checkout(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        assert gs._reclone(f"file://{bare}", None)["ok"]
        assert (roots / "top.sls").read_text() == "# seed\n"


@needs_git
def test_reclone_refuses_with_unpushed_work(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        (roots / "local.sls").write_text("# local\n")
        subprocess.run(["git", "-C", str(roots), "add", "local.sls"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(roots),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "local",
            ],
            check=True,
            capture_output=True,
        )
        assert not gs._reclone(f"file://{bare}", None)["ok"]
        assert (roots / "local.sls").is_file()  # nothing was destroyed


@needs_git
def test_set_remote_repoints_origin(ctx, tmp_path):
    app_ctx, roots = ctx
    first = _bare(tmp_path, "first.git")
    second = _bare(tmp_path, "second.git")
    _seed(first, tmp_path)
    with app_ctx:
        assert gs._clone(f"file://{first}", None)["ok"]
        with open(roots / "top.sls", "a") as fh:
            fh.write("# dirty\n")
        refused = gs._set_remote(f"file://{second}")
        assert not refused["ok"]  # dirty tracked tree: repoint orphans context
        subprocess.run(
            ["git", "-C", str(roots), "checkout", "--", "top.sls"], check=True
        )
        assert gs._set_remote(f"file://{second}")["ok"]
        out = subprocess.run(
            ["git", "-C", str(roots), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    assert out.stdout.strip() == f"file://{second}"


# --- routes ------------------------------------------------------------


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    roots = tmp_path / "srv" / "salt"
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
        session.commit()
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    monkeypatch.setattr(files_mod, "_refresh_fileserver", lambda: None)
    return app, client, roots


def _login_as(client, username):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": "pw"})


def _actions(app):
    with app.app_context():
        return [e.action for e in get_session().query(AuditEvent).all()]


def test_repo_view_offers_clone_when_empty(web_client):
    _, client, _roots = web_client
    rv = client.get("/files/repo")
    assert rv.status_code == 200
    assert b"Clone" in rv.data


def test_repo_clone_refuses_non_allowlisted_url(web_client):
    app, client, _roots = web_client
    rv = client.post(
        "/files/repo/clone",
        data={"url": "file:///etc/passwd"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"Clone refused" in rv.data
    assert any(a.startswith("git-clone") for a in _actions(app))


def test_repo_clone_success_audited(web_client, monkeypatch):
    app, client, _roots = web_client
    monkeypatch.setattr(files_mod, "clone_repo", lambda *a: {"ok": True})
    rv = client.post(
        "/files/repo/clone",
        data={"url": "https://git.example.com/s.git"},
    )
    assert rv.status_code == 302
    assert "git-clone" in _actions(app)


def test_repo_admin_only(web_client):
    _, client, _roots = web_client
    _login_as(client, "op")
    assert client.get("/files/repo").status_code == 403
    assert client.post("/files/repo/clone").status_code == 403
    assert client.get("/files/repo/clone").status_code == 405


@needs_git
def test_repo_reset_preview_then_confirm(web_client, tmp_path):
    app, client, roots = web_client
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    subprocess.run(["git", "clone", "-q", str(bare), str(roots)], check=True)
    (roots / "top.sls").write_text("# vandalized\n")
    preview = client.post("/files/repo/reset")
    assert preview.status_code == 200
    assert b"top.sls" in preview.data
    assert (roots / "top.sls").read_text() == "# vandalized\n"
    done = client.post("/files/repo/reset", data={"confirm": "1"})
    assert done.status_code == 302
    assert (roots / "top.sls").read_text() == "# seed\n"
    assert "git-reset" in _actions(app)


@needs_git
def test_repo_reclone_preview_names_destruction(web_client, tmp_path):
    _, client, roots = web_client
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    subprocess.run(["git", "clone", "-q", str(bare), str(roots)], check=True)
    (roots / "scratch.sls").write_text("# scratch\n")
    preview = client.post(
        "/files/repo/reclone",
        data={"url": "https://git.example.com/s.git"},
    )
    assert preview.status_code == 200
    assert b"destroy the checkout" in preview.data


@needs_git
def test_repo_reclone_confirm_audited(web_client, tmp_path, monkeypatch):
    app, client, _roots = web_client
    monkeypatch.setattr(files_mod, "reclone_repo", lambda *a: {"ok": True})
    rv = client.post(
        "/files/repo/reclone",
        data={
            "url": "https://git.example.com/s.git",
            "confirm": "1",
        },
    )
    assert rv.status_code == 302
    assert "git-reclone" in _actions(app)


@needs_git
def test_git_origin_reports_remote(web_client, tmp_path):
    app, _client, _roots = web_client
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app.app_context():
        assert gs.git_origin() is None
        assert gs._clone(f"file://{bare}", None)["ok"]
        assert gs.git_origin() == f"file://{bare}"

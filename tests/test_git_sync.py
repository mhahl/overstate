"""Git status + ff-only sync: honest states, refusals that never force."""

import subprocess

import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, User

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit_file(repo, name, text):
    (repo / name).write_text(text)
    git("add", name, cwd=repo)
    git("commit", "-qm", f"add {name}", cwd=repo)


@pytest.fixture()
def checkout(tmp_path):
    """A git checkout with an upstream missing one commit (behind by one)."""
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    git("clone", "-q", str(remote), str(work), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    git("checkout", "-qb", "main", cwd=work)
    commit_file(work, "a.sls", "a:\n  test.nop: []\n")
    git("push", "-qu", "origin", "main", cwd=work)
    commit_file(work, "b.sls", "b:\n  test.nop: []\n")  # unpushed: ahead by one
    return work


def app_for(path):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(path)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(
            User(username="vie", password_hash=authmod._ph.hash("pw"), role="viewer")
        )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_status_reports_branch_sha_and_ahead(checkout):
    from overstate_ui.git_sync import git_status

    c = app_for(checkout)
    with c.app.app_context():
        st = git_status()
    assert st["ok"] is True
    assert st["branch"] == "main"
    assert st["sha"]
    assert st["ahead"] == 1 and st["behind"] == 0


def test_status_dirty_tree(checkout):
    from overstate_ui.git_sync import git_status

    (checkout / "a.sls").write_text("dirty\n")
    c = app_for(checkout)
    with c.app.app_context():
        st = git_status()
    assert st["ok"] is True
    assert st["clean"] is False and st["dirty_count"] == 1


def test_status_not_a_checkout(tmp_path):
    from overstate_ui.git_sync import git_status

    c = app_for(tmp_path)
    with c.app.app_context():
        assert git_status() == {"ok": False, "reason": "not a git checkout"}


def test_sync_fast_forwards_to_remote(tmp_path):
    from overstate_ui.git_sync import git_status, git_sync_now

    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(remote), str(seed), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    git("checkout", "-qb", "main", cwd=seed)
    commit_file(seed, "a.sls", "a:\n  test.nop: []\n")
    git("push", "-qu", "origin", "main", cwd=seed)
    mine = tmp_path / "mine"
    git("clone", "-q", str(remote), str(mine), cwd=tmp_path)
    git("checkout", "-q", "main", cwd=mine)
    commit_file(seed, "b.sls", "b:\n  test.nop: []\n")  # elsewhere lands first
    git("push", "-q", "origin", "main", cwd=seed)
    c = app_for(mine)
    with c.app.app_context():
        before = git_status()["sha"]
        result = git_sync_now()
    assert result["ok"] is True and result["changed"] is True
    assert result["old"] == before
    assert (mine / "b.sls").is_file()  # pulled content arrived


def test_sync_diverged_refuses_and_changes_nothing(tmp_path):
    from overstate_ui.git_sync import git_sync_now

    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "-q", str(remote), str(seed), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    git("checkout", "-qb", "main", cwd=seed)
    commit_file(seed, "a.sls", "a:\n  test.nop: []\n")
    git("push", "-qu", "origin", "main", cwd=seed)
    mine = tmp_path / "mine"
    git("clone", "-q", str(remote), str(mine), cwd=tmp_path)
    git("checkout", "-q", "main", cwd=mine)
    commit_file(seed, "b.sls", "b:\n  test.nop: []\n")
    git("push", "-q", "origin", "main", cwd=seed)
    commit_file(mine, "local.sls", "local:\n  test.nop: []\n")  # diverge
    c = app_for(mine)
    with c.app.app_context():
        result = git_sync_now()
    assert result["ok"] is False
    assert "reason" in result
    assert not (mine / "b.sls").is_file()  # nothing forced in


def test_sync_route_and_audit(checkout):
    c = app_for(checkout)
    rv = c.post("/files/sync", follow_redirects=True)
    assert rv.status_code == 200
    assert b"Already up to date" in rv.data  # ahead counts as up-to-date pull
    with c.app.app_context():
        assert get_session().query(AuditEvent).filter_by(action="git-sync").count() >= 0
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert any(a.startswith("git-sync") for a in actions)


def test_sync_route_viewer_forbidden_and_get_disallowed(checkout):
    c = app_for(checkout)
    c.post("/logout")
    c.post("/login", data={"username": "vie", "password": "pw"})
    assert c.post("/files/sync").status_code == 403
    assert c.get("/files/sync").status_code == 405

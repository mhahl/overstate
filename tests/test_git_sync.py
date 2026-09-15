"""Git status + ff-only sync: honest states, refusals that never force."""

import json
import subprocess

import httpx
import pytest

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, User
from overstate_ui.salt_client import SaltClient

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
    """A git checkout one commit ahead of its upstream (unpushed work)."""
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


def _behind_checkout(tmp_path):
    """A checkout one commit behind its remote (something to pull)."""
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
    return mine


def _salt_stub(fileserver_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "runner" and body.get("fun") == "fileserver.update":
            if fileserver_ok:
                return httpx.Response(200, json={"return": [True]})
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"return": [{}]})

    return SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(handler)
    )


def _audit_actions(c):
    with c.app.app_context():
        return [row.action for row in get_session().query(AuditEvent).all()]


def _login(c, username):
    c.post("/logout")
    c.post("/login", data={"username": username, "password": "pw"})


def _add_operator(c):
    with c.app.app_context():
        get_session().add(
            User(
                username="op",
                password_hash=authmod._ph.hash("pw"),
                role="operator",
            )
        )
        get_session().commit()


def _remote_file(remote, ref_path):
    out = subprocess.run(
        ["git", "--git-dir", str(remote), "show", ref_path],
        capture_output=True,
        text=True,
        check=False,  # nonzero exit means "absent upstream", not an error
    )
    return out.stdout if out.returncode == 0 else None


def test_push_sends_ahead_commit_upstream(checkout):
    c = app_for(checkout)
    remote = checkout.parent / "remote.git"
    assert _remote_file(remote, "main:b.sls") is None  # unpushed work
    rv = c.post("/files/push", follow_redirects=True)
    assert rv.status_code == 200
    assert b"Pushed" in rv.data
    assert _remote_file(remote, "main:b.sls") is not None  # now upstream
    assert any(a.startswith("git-push:") for a in _audit_actions(c))


def test_push_second_time_is_up_to_date(checkout):
    c = app_for(checkout)
    c.post("/files/push")
    rv = c.post("/files/push", follow_redirects=True)
    assert b"Already up to date" in rv.data


def test_push_diverged_refuses_and_leaks_nothing(tmp_path):
    mine = _behind_checkout(tmp_path)
    git("config", "user.email", "t@t", cwd=mine)
    git("config", "user.name", "t", cwd=mine)
    commit_file(mine, "local.sls", "local:\n  test.nop: []\n")  # diverge
    c = app_for(mine)
    rv = c.post("/files/push", follow_redirects=True)
    assert rv.status_code == 200
    assert b"Push refused" in rv.data
    assert b"diverged" in rv.data
    assert str(tmp_path).encode() not in rv.data  # no local paths leak
    assert _remote_file(mine.parent / "remote.git", "main:local.sls") is None
    assert any(a.startswith("git-push-refused:") for a in _audit_actions(c))


def test_push_dirty_tree_refuses(checkout):
    (checkout / "a.sls").write_text("dirty\n")
    c = app_for(checkout)
    rv = c.post("/files/push", follow_redirects=True)
    assert b"dirty tree" in rv.data
    assert _remote_file(checkout.parent / "remote.git", "main:b.sls") is None


def test_push_no_upstream_refuses(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git("init", "-qb", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    commit_file(repo, "a.sls", "a:\n  test.nop: []\n")
    c = app_for(repo)
    rv = c.post("/files/push", follow_redirects=True)
    assert b"no upstream" in rv.data


def test_push_not_a_checkout_refuses(tmp_path):
    c = app_for(tmp_path)
    rv = c.post("/files/push", follow_redirects=True)
    assert b"not a git checkout" in rv.data


def test_push_operator_and_viewer_forbidden_and_get_disallowed(checkout):
    c = app_for(checkout)
    _add_operator(c)
    _login(c, "op")
    assert c.post("/files/push").status_code == 403
    _login(c, "vie")
    assert c.post("/files/push").status_code == 403
    _login(c, "admin")
    assert c.get("/files/push").status_code == 405


def test_push_button_admin_only(checkout):
    c = app_for(checkout)
    assert b"Push" in c.get("/files/").data  # admin sees it
    _add_operator(c)
    _login(c, "op")
    assert b"Push" not in c.get("/files/").data  # operator does not
    _login(c, "vie")
    assert b"Push" not in c.get("/files/").data  # viewer does not


def test_sync_changed_pull_refreshes_fileserver(tmp_path):
    c = app_for(_behind_checkout(tmp_path))
    c.app.extensions["salt_client"] = _salt_stub(fileserver_ok=True)
    rv = c.post("/files/sync", follow_redirects=True)
    assert rv.status_code == 200
    assert b"master fileserver refreshed" in rv.data
    actions = _audit_actions(c)
    assert any(a.startswith("git-sync:") for a in actions)
    assert "fileserver-update" in actions


def test_sync_changed_pull_refresh_failure_warns_not_fails(tmp_path):
    c = app_for(_behind_checkout(tmp_path))
    c.app.extensions["salt_client"] = _salt_stub(fileserver_ok=False)
    rv = c.post("/files/sync", follow_redirects=True)
    assert rv.status_code == 200
    assert b"Synced" in rv.data  # the pull landed
    assert b"master refresh failed" in rv.data  # refresh advisory only
    actions = _audit_actions(c)
    assert any(a.startswith("fileserver-update-failed:") for a in actions)


def test_fetch_reports_behind_without_pulling(tmp_path):
    mine = _behind_checkout(tmp_path)
    c = app_for(mine)
    rv = c.post("/files/fetch", follow_redirects=True)
    assert rv.status_code == 200
    assert b"1 commit(s) behind" in rv.data
    assert not (mine / "b.sls").is_file()  # fetch touches no files
    assert "git-fetch" in _audit_actions(c)


def test_fetch_up_to_date(checkout):
    c = app_for(checkout)
    rv = c.post("/files/fetch", follow_redirects=True)
    assert rv.status_code == 200
    assert b"up to date with the remote" in rv.data


def test_fetch_viewer_forbidden(checkout):
    c = app_for(checkout)
    c.post("/logout")
    c.post("/login", data={"username": "vie", "password": "pw"})
    assert c.post("/files/fetch").status_code == 403
    assert c.get("/files/fetch").status_code == 405


def test_git_failure_reason_never_carries_remote_secrets():
    from overstate_ui.git_sync import _failure

    class Proc:
        stderr = (
            "https://deploy:s3cr3t-token@git.example.com/org/states.git: fetch failed\n"
        )
        stdout = ""
        returncode = 1

    assert _failure(Proc(), "git fetch failed") == "git fetch failed"
    assert "s3cr3t-token" not in _failure(Proc(), "git fetch failed")

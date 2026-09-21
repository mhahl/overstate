"""Repo tab: clone a missing checkout, repoint origin, reset to remote.

The checkout lives at the srv roots (``salt/`` + ``pillar/`` beside
each other, file roots being the ``salt/`` child). Reads real git
behavior from local bare repos (file:// transport is a test-only path
straight into _clone, never through validation); the URL allowlist
itself is unit-tested against hostile inputs. argv capture proves user
input never reaches a flag position.
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
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# seed\n")
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
    _seed(bare, tmp_path, files=("salt/top.sls", "pillar/top.sls"))
    with app_ctx:
        result = gs._clone(f"file://{bare}", None)
    assert result["ok"], result
    srv = roots.parent
    assert (srv / ".git").is_dir()  # checkout lives at the srv roots
    assert (roots / "top.sls").is_file()  # salt tree served at file roots
    assert (srv / "pillar" / "top.sls").is_file()
    assert result.get("replaced", []) == []


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
    _seed(bare, tmp_path, files=("salt/top.sls",))
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        (roots / "top.sls").write_text("# vandalized\n")
        preview = gs.reset_preview()
        # Porcelain names paths from the checkout root (the srv level).
        assert preview["ok"] and "salt/top.sls" in preview["dirty"]
        result = gs.reset_hard(clean_untracked=False)
    assert result["ok"], result
    assert (roots / "top.sls").read_text() == "# seed\n"


@needs_git
def test_reset_refuses_when_diverged(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls",))
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
    _seed(bare, tmp_path, files=("salt/top.sls",))
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        live = roots.parent / "reactor" / "custom.sls"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("# live reactor\n")
        assert gs._reclone(f"file://{bare}", None)["ok"]
        assert (roots / "top.sls").read_text() == "# seed\n"
        assert live.read_text() == "# live reactor\n"  # sibling survives


@needs_git
def test_reclone_refuses_with_unpushed_work(ctx, tmp_path):
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls",))
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
    _seed(first, tmp_path, files=("salt/top.sls",))
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
    assert b'href="/files/"' in rv.data


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
    monkeypatch.setattr(files_mod, "clone_repo", lambda *a, **k: {"ok": True})
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
    _seed(bare, tmp_path, files=("salt/top.sls",))
    with app.app_context():
        assert gs.git_origin() is None
        assert gs._clone(f"file://{bare}", None)["ok"]
        assert gs.git_origin() == f"file://{bare}"


@needs_git
def test_clone_refuses_into_nonempty_non_checkout(ctx, tmp_path):
    """Foreign files at the srv roots: plain clone names the re-clone path."""
    app_ctx, roots = ctx
    srv = roots.parent
    (srv / "salt").mkdir(parents=True)
    (srv / "salt" / "seed.sls").write_text("# seed\n")
    (srv / "foreign.sls").write_text("# leftover\n")
    bare = _bare(tmp_path)
    _seed(bare, tmp_path)
    with app_ctx:
        assert not gs.is_checkout()
        result = gs._clone(f"file://{bare}", None)
    assert not result["ok"]
    assert result["reason"] == "directory not empty — re-clone to replace it"
    assert (srv / "foreign.sls").is_file()  # nothing was destroyed
    assert (srv / "salt" / "seed.sls").is_file()


@needs_git
def test_clone_replaces_seed_tree_only_after_confirm(ctx, tmp_path):
    """The deploy seed is replaceable, but never silently: the first call
    names every doomed file and changes nothing; the confirmed call
    installs and reports what it replaced."""
    app_ctx, roots = ctx
    srv = roots.parent
    (srv / "salt").mkdir(parents=True)
    (srv / "salt" / "demo.sls").write_text("# seed\n")
    (srv / "salt" / "top.sls").write_text("# seed\n")
    (srv / "reactor").mkdir(parents=True)
    (srv / "reactor" / "keep.sls").write_text("# live\n")
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls", "pillar/top.sls"))
    with app_ctx:
        first = gs._clone(f"file://{bare}", None)
    assert not first["ok"]
    assert first["confirm_replace"] == ["demo.sls", "top.sls"]
    assert (srv / "salt" / "demo.sls").is_file()  # nothing was destroyed
    with app_ctx:
        assert not gs.is_checkout()
        second = gs._clone(f"file://{bare}", None, replace=True)
    assert second["ok"], second
    assert second["replaced"] == ["demo.sls", "top.sls"]
    assert (roots / "top.sls").read_text() == "# seed\n"
    assert (srv / "pillar" / "top.sls").is_file()
    assert not (srv / "salt" / "demo.sls").exists()
    assert (srv / "reactor" / "keep.sls").is_file()  # sibling survives


@needs_git
def test_reclone_rescues_nonempty_non_checkout(ctx, tmp_path):
    """The advised path works: re-clone clears strays, then clones fresh."""
    app_ctx, roots = ctx
    roots.mkdir(parents=True)
    (roots / "stray.sls").write_text("# leftover\n")
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls",))
    with app_ctx:
        result = gs._reclone(f"file://{bare}", None)
    assert result["ok"], result
    assert (roots / "top.sls").read_text() == "# seed\n"
    assert not (roots / "stray.sls").exists()


def test_roots_nonempty_never_raises(ctx):
    app_ctx, roots = ctx
    with app_ctx:
        assert gs.roots_nonempty() is False  # missing dir reads as empty
        roots.mkdir(parents=True)
        assert gs.roots_nonempty() is False
        (roots / "stray.sls").write_text("# leftover\n")
        assert gs.roots_nonempty() is True


def test_repo_view_offers_reclone_when_roots_nonempty(web_client):
    _, client, roots = web_client
    roots.mkdir(parents=True)
    (roots / "stray.sls").write_text("# leftover\n")
    rv = client.get("/files/repo")
    assert rv.status_code == 200
    assert b"Re-clone from scratch" in rv.data
    assert b"not empty" in rv.data


def test_repo_view_hides_reclone_when_empty(web_client):
    _, client, _roots = web_client
    rv = client.get("/files/repo")
    assert rv.status_code == 200
    assert b"Clone" in rv.data
    assert b"Re-clone from scratch" not in rv.data


def test_repo_clone_refusal_prefills_reclone(web_client):
    """The dead end from the report: clone fails naming re-clone, and the
    page answers with the re-clone confirm one click away."""
    _, client, roots = web_client
    roots.parent.mkdir(parents=True)
    (roots.parent / "foreign.sls").write_text("# leftover\n")
    rv = client.post(
        "/files/repo/clone",
        data={"url": "https://git.example.com/salt/states.git"},
    )
    assert rv.status_code == 200  # stays on the page, not a redirect loop
    assert b"Clone refused" in rv.data
    assert b"re-clone to replace it" in rv.data
    assert b"Confirm re-clone" in rv.data
    assert b"https://git.example.com/salt/states.git" in rv.data
    assert (roots.parent / "foreign.sls").is_file()  # nothing was destroyed


def test_repo_clone_seed_replace_needs_confirm(web_client):
    """A seed salt/ tree is never replaced silently: the first post stays
    on the page naming the doomed files; the confirmed post clones."""
    _, client, roots = web_client
    roots.mkdir(parents=True)
    (roots / "demo.sls").write_text("# seed\n")
    rv = client.post(
        "/files/repo/clone",
        data={"url": "https://git.example.com/salt/states.git"},
    )
    assert rv.status_code == 200
    assert b"Confirm replace and clone" in rv.data
    assert b"demo.sls" in rv.data
    assert (roots / "demo.sls").is_file()  # nothing was destroyed


@needs_git
def test_repo_page_shows_entry_points(web_client, tmp_path):
    """The status card reports both Salt entry points once cloned."""
    app, client, _roots = web_client
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls", "pillar/top.sls"))
    with app.app_context():
        assert gs._clone(f"file://{bare}", None)["ok"]
    rv = client.get("/files/repo")
    assert rv.status_code == 200
    assert b"salt/top.sls" in rv.data
    assert b"pillar/top.sls" in rv.data


@needs_git
def test_status_ignores_token_helper(ctx, tmp_path):
    """The 0600 helper at the srv roots is deployment surface: a fresh
    clone with only the helper beside the salt tree still reads clean."""
    app_ctx, roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls",))
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        (roots.parent / ".git-credentials").write_text("https://x@t@h\n")
        st = gs.git_status()
    assert st["ok"] is True
    assert st["clean"] is True and st["dirty_count"] == 0


@needs_git
def test_checkout_layout_reports_entry_points(ctx, tmp_path):
    app_ctx, _roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("salt/top.sls", "pillar/top.sls"))
    with app_ctx:
        assert gs._clone(f"file://{bare}", None)["ok"]
        layout = gs.checkout_layout()
    assert layout == {
        "checkout": True,
        "at_roots": False,
        "salt_top": True,
        "pillar_top": True,
    }


@needs_git
def test_checkout_layout_marks_legacy_roots_checkout(ctx, tmp_path):
    """A hand-made checkout at file roots keeps working, but reports the
    legacy layout (pillar unserved) so the UI can nudge a re-clone."""
    app_ctx, _roots = ctx
    bare = _bare(tmp_path)
    _seed(bare, tmp_path, files=("top.sls",))
    subprocess.run(["git", "clone", "-q", str(bare), str(_roots)], check=True)
    with app_ctx:
        layout = gs.checkout_layout()
    assert layout["checkout"] is True
    assert layout["at_roots"] is True
    assert layout["salt_top"] is True
    assert layout["pillar_top"] is False


def test_ensure_world_readable_adds_bits(tmp_path):
    target = tmp_path / "tree"
    (target / "sub").mkdir(parents=True)
    locked = target / "sub" / "a.sls"
    locked.write_text("a\n")
    locked.chmod(0o600)
    (target / "sub").chmod(0o700)
    assert gs._ensure_world_readable(target) is True
    assert locked.stat().st_mode & 0o444 == 0o444
    assert (target / "sub").stat().st_mode & 0o555 == 0o555


@needs_git
def test_paths_changed_spots_module_updates(ctx, tmp_path):
    """The sync hint fires only when the pulled range touches _modules."""
    app_ctx, _roots = ctx
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)], check=True, cwd=tmp_path
    )
    seed = tmp_path / "seedwork2"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "-qb", "main"], check=True)
    (seed / "salt").mkdir()
    (seed / "salt" / "top.sls").write_text("# seed\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "-qu", "origin", "main"], check=True
    )
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    with app_ctx:
        assert gs._clone(f"file://{remote}", None)["ok"]
        old = gs.git_head()
        (seed / "salt" / "plain.sls").write_text("# plain\n")
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-qm", "plain"], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True
        )
        assert gs.git_sync_now()["ok"]
        new = gs.git_head()
        assert gs.paths_changed(old, new, "_modules") is False
        (seed / "salt" / "_modules").mkdir()
        (seed / "salt" / "_modules" / "m.py").write_text("# m\n")
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-qm", "mod"], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True
        )
        assert gs.git_sync_now()["ok"]
        assert gs.paths_changed(new, gs.git_head(), "_modules") is True

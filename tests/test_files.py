"""File browser tests: read-only listing/view, traversal rejection, and the
v4 edit/save/commit path (CodeMirror bundle with textarea fallback)."""

import re
import subprocess

import pytest

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.files import (
    highlight_json,
    highlight_yaml,
    list_tree,
    read_text,
    safe_join,
    sync_revision,
)
from overstate_ui.models import AuditEvent, User


@pytest.fixture()
def rooted(tmp_path):
    (tmp_path / "web.sls").write_text("nginx:\n  pkg.installed: []\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "db.sls").write_text("postgres:\n  pkg.installed: []\n")
    (tmp_path / "web.sls").write_text("nginx:\n  pkg.installed: []\n")
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(tmp_path)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_index_lists_files(rooted):
    rv = rooted.get("/files/")
    assert rv.status_code == 200
    assert b"web.sls" in rv.data and b"sub/db.sls" in rv.data


def test_view_renders_content(rooted):
    rv = rooted.get("/files/view", query_string={"path": "web.sls"})
    assert rv.status_code == 200
    assert b"pkg.installed" in rv.data
    assert b">1<" in rv.data  # line numbers


def test_view_highlights_yaml(rooted):
    rv = rooted.get("/files/view", query_string={"path": "web.sls"})
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "codehltable" in html  # highlighted block with line numbers
    assert '<span class="nt">nginx</span>' in html  # key token highlighted
    assert "pkg.installed" in html


def test_view_escapes_yaml_markup(rooted, tmp_path):
    (tmp_path / "evil.sls").write_text("key: '<script>alert(1)</script>'\n")
    rv = rooted.get("/files/view", query_string={"path": "evil.sls"})
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)" not in html


def test_view_plain_text_has_no_highlight(rooted, tmp_path):
    (tmp_path / "notes.txt").write_text("just some text\nsecond line\n")
    rv = rooted.get("/files/view", query_string={"path": "notes.txt"})
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "codehl" not in html
    assert "just some text" in html
    assert ">1<" in html  # plain line-number table kept


def test_highlight_yaml_keeps_content_verbatim():
    out = highlight_yaml("# comment\nnginx:\n  pkg.installed: []\n")
    assert "codehltable" in out
    assert '<span class="c1"># comment</span>' in out
    assert "pkg.installed" in out


def test_highlight_json_sorts_keys_and_highlights():
    out = highlight_json({"roles": ["web"], "zone": "east"})
    assert "codehltable" in out
    assert "roles" in out and "zone" in out
    # sorted keys: roles renders before zone
    assert out.index("roles") < out.index("zone")


def test_search_filters_listing(rooted):
    rv = rooted.get("/files/", query_string={"q": "db"})
    assert rv.status_code == 200
    assert b"sub/db.sls" in rv.data
    assert b"web.sls" not in rv.data
    rv = rooted.get("/files/", query_string={"q": "nothing-here"})
    assert b"No files match" in rv.data


def test_group_headers_and_top_hint(rooted, tmp_path):
    (tmp_path / "top.sls").write_text("base:\n  '*': []\n")
    rv = rooted.get("/files/")
    assert rv.status_code == 200
    assert b"sub/" in rv.data  # directory header row
    assert b"top.sls" in rv.data  # entry-point hint


def test_pagination_splits_many_files(rooted, tmp_path):
    for i in range(30):
        (tmp_path / f"bulk-{i:02d}.sls").write_text("x:\n  test.nop: []\n")
    first = rooted.get(
        "/files/", query_string={"per_page": "25", "page": "1"}
    ).data.decode()
    second = rooted.get(
        "/files/", query_string={"per_page": "25", "page": "2"}
    ).data.decode()
    assert "Page 1 of" in first and "Page 2 of" in second
    assert "bulk-29.sls" in second and "bulk-29.sls" not in first


def test_oversize_file_explains_instead_of_blank_404(rooted, tmp_path):
    (tmp_path / "huge.sls").write_bytes(b"x" * (257 * 1024))
    rv = rooted.get("/files/view", query_string={"path": "huge.sls"})
    assert rv.status_code == 200
    assert b"too large to display" in rv.data


def test_binary_file_explains_instead_of_blank_404(rooted, tmp_path):
    (tmp_path / "blob.sls").write_bytes(b"\xff\xfe\x00binary\x01\x02")
    rv = rooted.get("/files/view", query_string={"path": "blob.sls"})
    assert rv.status_code == 200
    assert b"not readable text" in rv.data


def test_traversal_rejected(rooted):
    for bad in ("../secret", "..", "/etc/passwd", "sub/../../x", ""):
        rv = rooted.get("/files/view", query_string={"path": bad})
        assert rv.status_code == 404, bad


def test_missing_file_404(rooted):
    assert (
        rooted.get("/files/view", query_string={"path": "nope.sls"}).status_code == 404
    )


def test_no_write_routes_exist(rooted):
    assert rooted.post("/files/").status_code == 405
    assert rooted.post("/files/view", data={"path": "x"}).status_code == 405


def test_requires_login():
    init_db("sqlite://")
    app = create_app(TestConfig)
    with app.app_context():
        create_all()
    c = app.test_client()
    assert c.get("/files/").status_code == 302


def test_safe_join_stays_inside(rooted, tmp_path):
    with rooted.app.app_context():
        assert safe_join("web.sls") is not None
        assert safe_join("../outside") is None
        assert safe_join("/etc/passwd") is None
        assert list_tree() != []
        assert read_text(safe_join("web.sls")).startswith("nginx")
        assert sync_revision() is None  # tmp dir is not a git checkout


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _commits(path):
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _committed_files(path):
    out = subprocess.run(
        ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line for line in out.stdout.splitlines() if line.strip())


@pytest.fixture()
def edit_checkout(tmp_path):
    """A git checkout (no upstream) with admin/operator/viewer users."""
    (tmp_path / "web.sls").write_text("nginx:\n  pkg.installed: []\n")
    (tmp_path / "notes.txt").write_text("just some text\n")
    _git(tmp_path, "init", "-qb", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed")
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(tmp_path)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(User(username="op", role="operator", password_hash=_ph.hash("pw")))
        session.add(User(username="vie", role="viewer", password_hash=_ph.hash("pw")))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def _login_as(c, username):
    c.post("/logout")
    c.post("/login", data={"username": username, "password": "pw"})


def _base_fields(c, path):
    """Read the edit form's concurrency fields as the browser would."""
    html = c.get("/files/edit", query_string={"path": path}).data.decode()
    return {
        name: re.search(rf'name="{name}" value="([^"]*)"', html).group(1)
        for name in ("base_sha", "base_hash")
    }


def test_edit_page_renders_editor_and_fallback(edit_checkout):
    rv = edit_checkout.get("/files/edit", query_string={"path": "web.sls"})
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "editor.bundle.js" in html  # vendored CodeMirror bundle
    assert 'id="editor-textarea"' in html  # no-JS fallback form field
    assert 'data-lang="yaml"' in html  # .sls gets YAML highlighting
    assert 'name="base_sha"' in html and 'name="base_hash"' in html


def test_edit_page_plain_text_lang(edit_checkout):
    html = edit_checkout.get(
        "/files/edit", query_string={"path": "notes.txt"}
    ).data.decode()
    assert 'data-lang="text"' in html


def test_editor_bundle_served_locally(rooted):
    rv = rooted.get("/static/editor.bundle.js")
    assert rv.status_code == 200
    assert len(rv.data) > 100_000  # real bundle, not a stub
    assert b"editor-mount" in rv.data  # our entry code is in the bundle


def test_edit_page_has_no_external_scripts(edit_checkout):
    rv = edit_checkout.get("/files/edit", query_string={"path": "web.sls"})
    assert rv.status_code == 200
    assert b'src="http' not in rv.data  # vendored only, CSP-safe


def test_edit_viewer_forbidden_and_anonymous_redirected(edit_checkout):
    _login_as(edit_checkout, "vie")
    assert (
        edit_checkout.get("/files/edit", query_string={"path": "web.sls"}).status_code
        == 403
    )
    assert (
        edit_checkout.post(
            "/files/save", data={"path": "web.sls", "content": "x"}
        ).status_code
        == 403
    )
    edit_checkout.post("/logout")
    assert (
        edit_checkout.get("/files/edit", query_string={"path": "web.sls"}).status_code
        == 302
    )


def test_edit_traversal_and_missing_404(edit_checkout):
    for bad in ("../secret", "/etc/passwd", "nope.sls"):
        assert (
            edit_checkout.get("/files/edit", query_string={"path": bad}).status_code
            == 404
        ), bad


def test_edit_uneditable_file_redirects_to_view(edit_checkout, tmp_path):
    (tmp_path / "blob.sls").write_bytes(b"\xff\xfe\x00binary\x01\x02")
    rv = edit_checkout.get("/files/edit", query_string={"path": "blob.sls"})
    assert rv.status_code == 302  # back to the explainer card, nothing to edit


def test_save_round_trip_commits_single_file(edit_checkout, tmp_path):
    before = _commits(tmp_path)
    fields = _base_fields(edit_checkout, "web.sls")
    rv = edit_checkout.post(
        "/files/save",
        data={
            "path": "web.sls",
            "content": "nginx:\n  pkg.installed: []\n# edited\n",
            **fields,
        },
    )
    assert rv.status_code == 302
    assert (tmp_path / "web.sls").read_text().endswith("# edited\n")
    assert _commits(tmp_path) == before + 1  # exactly one local commit
    assert _committed_files(tmp_path) == ["web.sls"]  # touching only that file
    shown = edit_checkout.get(rv.headers["Location"]).data.decode()
    assert "committed as" in shown  # save flash names the commit, not a sync
    with edit_checkout.app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert any(a.startswith("file-save:web.sls:") for a in actions)


def test_save_identical_content_commits_nothing(edit_checkout, tmp_path):
    before = _commits(tmp_path)
    fields = _base_fields(edit_checkout, "web.sls")
    rv = edit_checkout.post(
        "/files/save",
        data={
            "path": "web.sls",
            "content": "nginx:\n  pkg.installed: []\n",
            **fields,
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"No changes" in rv.data
    assert _commits(tmp_path) == before


def test_save_traversal_and_missing_404(edit_checkout):
    fields = {"base_sha": "x", "base_hash": "y"}
    for bad in ("../secret", "/etc/passwd", "nope.sls"):
        assert (
            edit_checkout.post(
                "/files/save", data={"path": bad, "content": "x", **fields}
            ).status_code
            == 404
        ), bad


def test_save_binary_and_oversize_refused(edit_checkout, tmp_path):
    (tmp_path / "blob.sls").write_bytes(b"\xff\xfe\x00binary\x01\x02")
    (tmp_path / "huge.sls").write_bytes(b"x" * (257 * 1024))
    before = _commits(tmp_path)
    fields = {"base_sha": "x", "base_hash": "y"}
    for name, body in (
        ("blob.sls", "replacement"),
        ("huge.sls", "x" * (257 * 1024)),
    ):
        rv = edit_checkout.post(
            "/files/save", data={"path": name, "content": body, **fields}
        )
        assert rv.status_code == 302
    assert _commits(tmp_path) == before  # nothing committed
    assert (tmp_path / "blob.sls").read_bytes().startswith(b"\xff\xfe")


def test_save_outside_checkout_refused(rooted, tmp_path):
    before = (tmp_path / "web.sls").read_text()
    rv = rooted.post(
        "/files/save",
        data={
            "path": "web.sls",
            "content": "changed\n",
            "base_sha": "x",
            "base_hash": "y",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"Not a git checkout" in rv.data
    assert (tmp_path / "web.sls").read_text() == before  # untouched


def test_save_stale_base_refuses_without_writing(edit_checkout, tmp_path):
    fields = _base_fields(edit_checkout, "web.sls")
    # Someone else (or a sync) lands first: new content, new HEAD.
    (tmp_path / "web.sls").write_text("nginx:\n  pkg.installed: []\n# elsewhere\n")
    _git(tmp_path, "add", "web.sls")
    _git(tmp_path, "commit", "-qm", "elsewhere")
    before = _commits(tmp_path)
    rv = edit_checkout.post(
        "/files/save",
        data={
            "path": "web.sls",
            "content": "nginx:\n  pkg.installed: []\n# mine\n",
            **fields,
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"changed underneath you" in rv.data
    assert (tmp_path / "web.sls").read_text().endswith("# elsewhere\n")
    assert _commits(tmp_path) == before  # nothing committed


def test_save_invalid_yaml_warns_but_saves(edit_checkout, tmp_path):
    fields = _base_fields(edit_checkout, "web.sls")
    rv = edit_checkout.post(
        "/files/save",
        data={"path": "web.sls", "content": "key: [unclosed\n", **fields},
        follow_redirects=True,
    )
    assert b"committed as" in rv.data
    assert b"does not parse" in rv.data
    assert (tmp_path / "web.sls").read_text() == "key: [unclosed\n"


def test_save_valid_yaml_has_no_warning(edit_checkout):
    fields = _base_fields(edit_checkout, "web.sls")
    rv = edit_checkout.post(
        "/files/save",
        data={
            "path": "web.sls",
            "content": "nginx:\n  pkg.installed: []\n# ok\n",
            **fields,
        },
        follow_redirects=True,
    )
    assert b"committed as" in rv.data
    assert b"does not parse" not in rv.data


def test_commit_metadata_collapses_to_one_line():
    from overstate_ui.git_sync import _oneline

    assert _oneline("op\ninjected", 64) == "op injected"
    assert _oneline("a<b>c", 64) == "a b c"
    assert len(_oneline("x" * 200, 64)) == 64
    assert _oneline("", 64) == ""


def test_git_writes_serialize_on_busy_lock(edit_checkout):
    from overstate_ui import git_sync

    git_sync._sync_lock.acquire()
    try:
        with edit_checkout.app.app_context():
            assert git_sync.git_commit_file("web.sls", "s", "t") == {
                "ok": False,
                "reason": "a sync is already running",
            }
            assert git_sync.git_push_now()["reason"] == "a sync is already running"
    finally:
        git_sync._sync_lock.release()


def test_stale_and_uncommitted_saves_leave_audit_rows(
    edit_checkout, tmp_path, monkeypatch
):
    fields = _base_fields(edit_checkout, "web.sls")
    (tmp_path / "web.sls").write_text("nginx:\n  pkg.installed: []\n# elsewhere\n")
    edit_checkout.post(
        "/files/save",
        data={"path": "web.sls", "content": "mine\n", **fields},
    )
    import overstate_ui.files as filesmod

    monkeypatch.setattr(
        filesmod, "git_commit_file", lambda *a: {"ok": False, "reason": "boom"}
    )
    fields = _base_fields(edit_checkout, "web.sls")
    edit_checkout.post(
        "/files/save",
        data={"path": "web.sls", "content": "written anyway\n", **fields},
    )
    assert (tmp_path / "web.sls").read_text() == "written anyway\n"
    with edit_checkout.app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert "file-save-refused:web.sls:stale" in actions
    assert any(a.startswith("file-save-uncommitted:web.sls:") for a in actions)


def test_third_party_licenses_list_the_editor():
    import pathlib

    text = (
        pathlib.Path(__file__).resolve().parent.parent / "THIRD-PARTY-LICENSES.md"
    ).read_text()
    assert "codemirror" in text.lower()

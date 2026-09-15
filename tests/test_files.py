"""v2 unit 2 tests: read-only file browser, traversal rejection, no writes."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db
from overstate_ui.files import (
    highlight_yaml,
    list_tree,
    read_text,
    safe_join,
    sync_revision,
)


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

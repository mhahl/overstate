"""v2 unit 2 tests: read-only file browser, traversal rejection, no writes."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db
from overstate_ui.files import list_tree, read_text, safe_join, sync_revision


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


def test_traversal_rejected(rooted):
    for bad in ("../secret", "..", "/etc/passwd", "sub/../../x", ""):
        rv = rooted.get("/files/view", query_string={"path": bad})
        assert rv.status_code == 404, bad


def test_missing_file_404(rooted):
    assert rooted.get("/files/view", query_string={"path": "nope.sls"}).status_code == 404


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

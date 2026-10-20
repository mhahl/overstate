"""Users section tabs: one sidebar entry, two tabbed pages.

Access groups moved under the Users sidebar entry; both routes live in
the users blueprint, so exactly one sidebar item and exactly one tab are
active at a time. Mirrors the Pillar section tab tests.
"""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User

PW = "pw"


@pytest.fixture()
def env():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        get_session().add(
            User(username="op", role="operator", password_hash=_ph.hash(PW))
        )
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": PW})
    c.app = app
    return c


def _tab_bar(html):
    return html.split('aria-label="Users views"', 1)[1].split("</div>", 1)[0].split('role="tab"')[1:]


def _aside(html):
    return html.split('id="nav-panel"', 1)[1].split("</aside>", 1)[0]


def test_sidebar_has_one_users_entry(env):
    for path in ("/users/", "/users/groups"):
        html = env.get(path).data.decode()
        aside = _aside(html)
        assert 'href="/users/"' in aside
        # Access groups is a tab now, not a second sidebar item.
        assert 'href="/users/groups"' not in aside
        assert aside.count("menu-active") == 1


def test_tabs_mark_exactly_one_active(env):
    users = _tab_bar(env.get("/users/").data.decode())
    assert len(users) == 2
    assert "tab-active" in users[0] and "/users/groups" not in users[0]
    assert "tab-active" not in users[1] and "/users/groups" in users[1]
    assert 'aria-current="page"' in users[0]

    groups = _tab_bar(env.get("/users/groups").data.decode())
    assert len(groups) == 2
    assert "tab-active" not in groups[0] and "/users/groups" not in groups[0]
    assert "tab-active" in groups[1] and "/users/groups" in groups[1]
    assert 'aria-current="page"' in groups[1]

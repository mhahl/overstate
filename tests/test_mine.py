"""Mine browser tests: values table, empty state, denial flash,
viewer reads, saved-group resolution."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Minion, MinionGroup
from overstate_ui.salt_client import SaltClient

CALLS: list = []
DENY_MINE = False


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["web-01"],
                                    "minions_pre": [],
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        if body.get("client") == "runner":
            return httpx.Response(
                200, json={"return": [{"up": ["web-01"], "down": []}]}
            )
        if body.get("client") == "local" and body.get("fun") == "mine.get":
            CALLS.append(body)
            if DENY_MINE:
                return httpx.Response(500, json={})
            fun = (body.get("arg") or ["*", ""])[1]
            data = {"web-01": ["10.0.0.1"]} if fun == "network.ip_addrs" else {}
            return httpx.Response(200, json={"return": [{body.get("tgt"): data}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(Minion(id="web-01", key_status="accepted", grains={}))
        get_session().add(MinionGroup(name="g", members=["web-01"]))
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def query(c, **params):
    return c.get(
        "/mine/",
        query_string={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "network.ip_addrs",
            **params,
        },
    )


def test_index_shows_form_without_query():
    html = make_client().get("/mine/").data.decode()
    assert "Mine" in html and "network.ip_addrs" in html
    assert "web-01" not in html  # no query ran yet


def test_values_table_and_staleness_note():
    CALLS.clear()
    html = query(make_client()).data.decode()
    assert "web-01" in html and "10.0.0.1" in html
    assert "cached" in html and "mine-update" in html
    assert CALLS and CALLS[0]["fun"] == "mine.get"


def test_minion_sort_direction_toggles():
    html = query(make_client(), dir="desc").data.decode()
    assert 'aria-sort="desc"' in html
    assert "dir=asc" in html  # header link flips back


def test_empty_function_explains():
    html = query(make_client(), fun="nosuch").data.decode()
    assert "nothing stored" in html
    assert "mine_functions" in html
    assert "web-01" not in html


def test_denial_flashes_error():
    global DENY_MINE
    DENY_MINE = True
    try:
        html = query(make_client()).data.decode()
    finally:
        DENY_MINE = False
    assert "salt-api error" in html


def test_group_target_resolves_to_list():
    CALLS.clear()
    html = (
        make_client()
        .get(
            "/mine/",
            query_string={"tgt": "g", "tgt_type": "group", "fun": "network.ip_addrs"},
        )
        .data.decode()
    )
    assert "web-01" in html
    assert CALLS and CALLS[0]["arg"][0] == "web-01"
    assert CALLS[0]["kwarg"]["tgt_type"] == "list"


def test_viewer_can_read():
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    c = make_client()
    with c.app.app_context():
        get_session().add(
            User(username="v", password_hash=_ph.hash("pw"), role="viewer")
        )
        get_session().commit()
    viewer = c.app.test_client()
    viewer.post("/login", data={"username": "v", "password": "pw"})
    rv = viewer.get(
        "/mine/",
        query_string={"tgt": "*", "tgt_type": "glob", "fun": "network.ip_addrs"},
    )
    assert rv.status_code == 200
    assert "web-01" in rv.data.decode()


def test_minion_tab_prompts_without_query():
    CALLS.clear()
    html = make_client().get("/minions/web-01?tab=mine").data.decode()
    assert "Mine" in html
    assert not CALLS  # no Salt call until a function is entered


def test_minion_tab_shows_stored_value():
    html = (
        make_client()
        .get("/minions/web-01?tab=mine&mine_fun=network.ip_addrs")
        .data.decode()
    )
    assert "10.0.0.1" in html


def test_minion_tab_empty_explains():
    html = make_client().get("/minions/web-01?tab=mine&mine_fun=nosuch").data.decode()
    assert "nothing stored" in html
    assert "mine_functions" in html


def test_minion_tab_denial_shows_error():
    global DENY_MINE
    DENY_MINE = True
    try:
        html = (
            make_client()
            .get("/minions/web-01?tab=mine&mine_fun=network.ip_addrs")
            .data.decode()
        )
    finally:
        DENY_MINE = False
    # detail() renders the raw exception text, without a prefix
    assert "salt-api call failed" in html

"""Function browser tests: live index merged into the run-form
combobox, lazy sys.doc fragment, denial fallback, viewer reads."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Minion
from overstate_ui.salt_client import SaltClient

DENY_LIST = False


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
        if body.get("client") == "local":
            fun = body.get("fun")
            if fun == "sys.list_functions":
                if DENY_LIST:
                    return httpx.Response(500, json={})
                return httpx.Response(
                    200,
                    json={"return": [{"web-01": ["test.ping", "network.interfaces"]}]},
                )
            if fun == "sys.doc":
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                "web-01": {
                                    "network.interfaces": "Return interfaces and respective addresses."
                                }
                            }
                        ]
                    },
                )
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
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_new_merges_live_functions_presets_first():
    html = make_client().get("/jobs/new").data.decode()
    assert "network.interfaces" in html  # live index beyond presets
    assert html.index("test.ping") < html.index("network.interfaces")


def test_fun_doc_fragment():
    c = make_client()
    rv = c.get(
        "/jobs/fun-doc", query_string={"fun": "network.interfaces", "minion": "web-01"}
    )
    assert rv.status_code == 200
    assert "Return interfaces" in rv.data.decode()


def test_fun_doc_rejects_bad_name():
    c = make_client()
    rv = c.get("/jobs/fun-doc", query_string={"fun": "x;rm -rf", "minion": "web-01"})
    assert rv.status_code == 400


def test_list_denial_falls_back_to_presets():
    global DENY_LIST
    DENY_LIST = True
    try:
        html = make_client().get("/jobs/new").data.decode()
    finally:
        DENY_LIST = False
    assert "test.ping" in html  # presets still render
    assert "network.interfaces" not in html


def test_viewer_can_read_fun_doc():
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
        "/jobs/fun-doc", query_string={"fun": "test.ping", "minion": "web-01"}
    )
    assert rv.status_code == 200

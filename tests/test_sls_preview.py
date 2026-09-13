"""SLS preview tests: review page renders show_sls output per
requested file; denial degrades to a note with Fire intact."""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.salt_client import SaltClient
from overstate_ui.seed_mock import seed as seed_mock

DENY_SHOW_SLS = False

STATES = {"/tmp/overstate-demo.txt": {
    "file": [{"contents": "managed"}, "managed", {"order": 10000}],
    "__sls__": "demo", "__env__": "base"}}


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"return": [{"token": "tok",
                                                         "expire": 99}]})
        body = json.loads(request.content or b"{}")
        if body.get("client") == "wheel":
            return httpx.Response(200, json={"return": [{"data": {"return": {
                "minions": ["fedora-web-01"], "minions_pre": [],
                "minions_rejected": [], "minions_denied": []}}}]})
        if body.get("client") == "runner":
            return httpx.Response(200, json={"return": [{
                "up": ["fedora-web-01"], "down": []}]})
        if body.get("client") in ("local", "ssh"):
            if body.get("fun") == "state.show_sls":
                if DENY_SHOW_SLS:
                    return httpx.Response(500, json={})
                return httpx.Response(200, json={"return": [{
                    body.get("tgt"): STATES}]})
        if body.get("client") == "local_async":
            return httpx.Response(200, json={"return": [{
                "jid": "424242", "minions": ["fedora-web-01"]}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        seed_mock(get_session())
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def confirm(c, fun="state.apply", args="demo", **extra):
    data = {"tgt": "*", "tgt_type": "glob", "fun": fun, "args": args,
            "mode": "async"}
    data.update(extra)
    return c.post("/jobs/run", data=data)


def test_confirm_shows_sls_preview():
    html = confirm(make_client()).data.decode()
    assert "SLS preview" in html
    assert "/tmp/overstate-demo.txt" in html
    assert "fedora-db-01" in html  # first matched minion renders
    assert "Run state.apply" in html  # Fire stays available


def test_preview_denial_renders_note_fire_intact():
    global DENY_SHOW_SLS
    DENY_SHOW_SLS = True
    try:
        html = confirm(make_client()).data.decode()
    finally:
        DENY_SHOW_SLS = False
    assert "Preview unavailable" in html
    assert "Run state.apply" in html


def test_no_preview_for_other_functions():
    html = confirm(make_client(), fun="pkg.install",
                   args="nginx").data.decode()
    assert "SLS preview" not in html
    assert "Run pkg.install" in html


def test_no_preview_for_test_mode():
    rv = confirm(make_client(), args="demo test=True")
    assert rv.status_code == 302  # skips review entirely, as before


def test_show_sls_now_shapes():
    from overstate_ui.tasks import show_sls_now

    class Stub:
        def __init__(self, payload):
            self.payload = payload

        def local(self, *a, **k):
            return [self.payload]

    out = show_sls_now(Stub({"m": STATES}), "m", ["demo"], "local")
    assert out == {"demo": STATES}
    assert show_sls_now(Stub({"m": None}), "m", ["demo"], "local") == {}
    assert show_sls_now(Stub({}), "m", ["demo"], "local") == {}

"""Phase 2 tests: salt_client against a faked salt-api, dashboard live/offline."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.dashboard import collect_stats
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.salt_client import SaltApiError, SaltClient


def fake_transport(state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 43200}]}
            )
        token = request.headers.get("X-Auth-Token")
        if token != "tok" and not state.get("expired_ok"):
            return httpx.Response(401, json={"error": "auth"})
        try:
            body = __import__("json").loads(request.content or b"{}")
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("client") == "wheel":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["a", "b"],
                                    "minions_pre": ["c"],
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        if isinstance(body, dict) and body.get("client") == "runner":
            return httpx.Response(200, json={"return": [{"up": ["a"], "down": ["b"]}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client(state: dict | None = None) -> SaltClient:
    return SaltClient(
        "https://salt:8000", "overstate", "pw", transport=fake_transport(state or {})
    )


def test_wheel_login_and_call():
    stats = make_client().wheel("key.list_all")
    assert stats[0]["data"]["return"]["minions"] == ["a", "b"]


def test_local_forwards_kwargs():
    import json as _json

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        seen.update(_json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"return": [{"web-01": True}]})

    client = SaltClient(
        "https://salt:8000", "u", "p", transport=httpx.MockTransport(handler)
    )
    out = client.local(
        "web-01",
        "schedule.add",
        arg=["daily"],
        kwarg={"function": "test.ping", "seconds": 60},
    )
    assert out == [{"web-01": True}]
    assert seen["arg"] == ["daily"]
    assert seen["kwarg"] == {"function": "test.ping", "seconds": 60}


def test_401_triggers_relogin():
    bad = httpx.MockTransport(
        lambda req: (
            httpx.Response(401, json={"error": "expired"})
            if req.url.path != "/login"
            else httpx.Response(200, json={"return": [{"token": "tok2", "expire": 1}]})
        )
    )
    client = SaltClient("https://salt:8000", "u", "p", transport=bad)
    with pytest.raises(SaltApiError):
        client.wheel("key.list_all")  # token invalid even after relogin
    assert client._token == "tok2"


def test_health_ok_and_down():
    assert make_client().health()["reachable"] is True
    down = SaltClient(
        "https://salt:8000",
        "u",
        "p",
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom")),
    )
    health = down.health()
    assert health["reachable"] is False
    assert health["error"]


@pytest.fixture()
def app_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = make_client()
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


def test_dashboard_live(app_client):
    rv = app_client.get("/")
    assert rv.status_code == 200
    html = rv.data.decode()
    assert "1 / 1" in html  # up / down
    assert "salt-api health" in html


def test_dashboard_offline_degrades():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000",
        "u",
        "p",
        transport=httpx.MockTransport(lambda req: httpx.Response(500)),
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    rv = client.get("/")
    assert rv.status_code == 200
    assert "unreachable" in rv.data.decode()


def test_collect_stats_db_counts():
    from overstate_ui.seed_mock import seed as _seed

    init_db("sqlite://")
    app = create_app(TestConfig)
    with app.app_context():
        create_all()
        _seed(get_session())
        stats = collect_stats(make_client())
    assert stats["accepted"] == 2
    assert stats["pending"] == 1
    assert stats["in_flight"] == 1
    assert len(stats["last_failures"]) == 1

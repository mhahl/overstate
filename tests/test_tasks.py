"""Worker, capability, and rotation tests: queue fallback, probes, verify."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.salt_client import SaltApiError, SaltClient


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={
                "return": [{"token": "tok", "expire": 99999}]})
        body = __import__("json").loads(request.content or b"{}")
        fun = body.get("fun", "")
        if body.get("client") == "wheel" and fun == "key.list_all":
            return httpx.Response(200, json={
                "return": [{"data": {"return": {
                    "minions": ["m1"], "minions_pre": []}}}]})
        if body.get("client") == "runner" and fun == "manage.status":
            return httpx.Response(200, json={
                "return": [{"up": ["m1"], "down": []}]})
        if fun == "grains.items":
            return httpx.Response(200, json={
                "return": [{"m1": {"osfinger": "TestOS",
                                   "ipv4": ["10.0.0.1"]}}]})
        if fun == "test.ping":
            return httpx.Response(200, json={
                "return": [{body.get("tgt"): True}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["REDIS_URL"] = "redis://127.0.0.1:9/0"
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport())
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


@pytest.fixture()
def admin(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


class StubClient:
    def __init__(self, deny=()):
        self.deny = set(deny)

    def wheel(self, fun, **kwargs):
        if fun in self.deny:
            raise SaltApiError("denied")
        return [{"data": {"return": {"minions": ["m1"],
                                     "minions_pre": ["m2"]}}}]

    def runner(self, fun, **kwargs):
        if fun in self.deny:
            raise SaltApiError("denied")
        if fun == "manage.status":
            return [{"up": ["m1"], "down": []}]
        return [{}]

    def local(self, tgt, fun, **kwargs):
        if fun in self.deny:
            raise SaltApiError("denied")
        return [{tgt: True}]


class StubJob:
    def __init__(self, state, value=None):
        self._state = state
        self.result = value
        self.exc_info = "Traceback\nRuntimeError: boom" if state == "failed" else ""

    def refresh(self):
        pass

    def get_status(self):
        return self._state


def test_queue_or_none_returns_none_without_redis(app):
    from overstate_ui.tasks import queue_or_none, salt_overview_task

    with app.app_context():
        assert queue_or_none(salt_overview_task) is None


def test_wait_for_ready_error_and_timeout():
    from overstate_ui.tasks import wait_for

    assert wait_for(StubJob("finished", {"count": 3})) == ("ready", {"count": 3})
    status, value = wait_for(StubJob("failed"))
    assert status == "error" and "boom" in value
    assert wait_for(StubJob("started"), wait=0) == ("pending", None)


def test_probe_capabilities_all_doors(app):
    from overstate_ui.tasks import CAPABILITY_CHECKS, probe_capabilities

    with app.app_context():
        out = probe_capabilities(StubClient(), ping_target="m1")
    assert {c["key"] for c in CAPABILITY_CHECKS} <= set(out)
    assert out["wheel_ok"] and out["runner_ok"]
    assert out["history_ok"] and out["ping_ok"]
    assert out["ping_target"] == "m1" and out["error"] is None


def test_probe_capabilities_denied_and_no_target(app):
    from overstate_ui.tasks import probe_capabilities

    with app.app_context():
        out = probe_capabilities(StubClient(deny={"key.list_all"}))
    assert out["wheel_ok"] is False
    assert out["runner_ok"] is False  # never reached past the first denial
    assert out["ping_ok"] is False and out["error"]
    with app.app_context():
        skipped = probe_capabilities(StubClient())
    assert skipped["ping_ok"] is False and skipped["ping_target"] is None


def test_isolated_app_teardown_uses_live_registry(app):
    """Fresh worker processes start with db._Session unset; teardown
    must use the registry rebound by create_app, not a stale import."""
    import overstate_ui.db as dbmod
    from overstate_ui.tasks import isolated_app

    saved = (dbmod._engine, dbmod._Session)
    dbmod._engine, dbmod._Session = None, None
    try:
        with isolated_app():
            assert dbmod._Session is not None
    finally:
        dbmod._engine, dbmod._Session = saved


def test_capability_checks_carry_guidance():
    from overstate_ui.tasks import CAPABILITY_CHECKS

    for check in CAPABILITY_CHECKS:
        assert check["feature"] and check["fun"] and check["grant"]


def test_salt_overview_parses_and_raises(app):
    from overstate_ui.tasks import salt_overview_now

    with app.app_context():
        out = salt_overview_now(StubClient())
    assert out == {"reachable": True, "accepted": 1, "pending": 1,
                   "up": 1, "down": 0}
    with app.app_context(), pytest.raises(SaltApiError):
        salt_overview_now(StubClient(deny={"key.list_all"}))


def test_refresh_uses_worker_result(monkeypatch, admin):
    monkeypatch.setattr("overstate_ui.tasks.queue_or_none",
                        lambda *a, **k: StubJob("finished", {"count": 3}))
    rv = admin.post("/minions/refresh", follow_redirects=True)
    assert "Inventory refreshed: 3 minions." in rv.data.decode()


def test_dashboard_renders_capability_checklist(app, admin):
    from overstate_ui.models import Minion

    with app.app_context():
        get_session().add(Minion(id="m1", grains={}, conformity={}))
        get_session().commit()
    html = admin.get("/").data.decode()
    assert "Capabilities" in html
    assert "Keys" in html and "Job history" in html
    assert "Run jobs" in html and "Presence" in html
    assert html.count("badge-success") >= 4


def test_rotation_page_and_verify(monkeypatch, admin):
    html = admin.get("/users/rotation").data.decode()
    assert "Rotate salt-api password" in html

    class LoginOk:
        def login(self):
            return None

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginOk())
    rv = admin.post("/users/rotation/verify", data={"password": "new"},
                    follow_redirects=True)
    assert "New eauth password works." in rv.data.decode()

    class LoginDenied:
        def login(self):
            raise SaltApiError("denied")

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginDenied())
    rv = admin.post("/users/rotation/verify", data={"password": "bad"},
                    follow_redirects=True)
    assert "verification failed" in rv.data.decode()


def test_rotation_forbidden_for_operator(app):
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(User(username="op", password_hash="x",
                               role="operator"))
        get_session().commit()
        op_id = get_session().query(User).filter_by(
            username="op").first().id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(op_id)
    assert client.get("/users/rotation").status_code == 403
    assert client.post("/users/rotation/verify").status_code == 403

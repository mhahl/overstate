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
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99999}]}
            )
        body = __import__("json").loads(request.content or b"{}")
        fun = body.get("fun", "")
        if body.get("client") == "wheel" and fun == "key.list_all":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {"data": {"return": {"minions": ["m1"], "minions_pre": []}}}
                    ]
                },
            )
        if body.get("client") == "runner" and fun == "manage.status":
            return httpx.Response(200, json={"return": [{"up": ["m1"], "down": []}]})
        if fun == "grains.items":
            return httpx.Response(
                200,
                json={"return": [{"m1": {"osfinger": "TestOS", "ipv4": ["10.0.0.1"]}}]},
            )
        if fun == "test.ping":
            return httpx.Response(200, json={"return": [{body.get("tgt"): True}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["REDIS_URL"] = "redis://127.0.0.1:9/0"
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
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
        return [{"data": {"return": {"minions": ["m1"], "minions_pre": ["m2"]}}}]

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
    from overstate_ui.tasks import fleet_keys_task, queue_or_none

    with app.app_context():
        assert queue_or_none(fleet_keys_task) is None


def test_wait_for_ready_error_and_timeout():
    from overstate_ui.tasks import wait_for

    assert wait_for(StubJob("finished", {"count": 3})) == ("ready", {"count": 3})
    status, value = wait_for(StubJob("failed"))
    assert (status, value) == ("error", "worker failed")
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
        out = probe_capabilities(StubClient(deny={"key.list_all"}), ping_target="m1")
    assert out["wheel_ok"] is False
    assert out["runner_ok"] is True  # doors are independent: one denial blanks one door
    assert out["ping_ok"] is True and out["error"]
    with app.app_context():
        skipped = probe_capabilities(StubClient())
    assert skipped["ping_ok"] is False and skipped["ping_target"] is None


def test_probe_ping_uses_short_salt_timeout(app):
    """The ping door must not wait out a dead minion: it carries a short
    Salt job timeout instead of relying on the HTTP backstop."""
    from overstate_ui.tasks import probe_capabilities
    from overstate_ui.tasks_salt import PING_SALT_TIMEOUT

    seen: dict = {}

    class RecordingClient(StubClient):
        def local(self, tgt, fun, **kwargs):
            seen.update(kwargs)
            return super().local(tgt, fun, **kwargs)

    with app.app_context():
        out = probe_capabilities(RecordingClient(), ping_target="m1")
    assert out["ping_ok"] is True
    assert seen.get("timeout") == PING_SALT_TIMEOUT


def test_ping_hint_without_target_points_at_enrollment():
    from overstate_ui.dashboard import capability_checks

    checks = capability_checks({"ping_ok": False, "ping_target": None})
    ping = next(c for c in checks if c["key"] == "ping_ok")
    assert not ping["ok"]
    assert "minion" in ping["grant"].lower()
    assert "grant" not in ping["grant"].lower()


def test_ping_failure_with_target_keeps_grant_guidance():
    from overstate_ui.dashboard import capability_checks

    checks = capability_checks({"ping_ok": False, "ping_target": "m1"})
    ping = next(c for c in checks if c["key"] == "ping_ok")
    assert not ping["ok"]
    assert ping["grant"] == "Grant execution functions to the eauth user"


def test_isolated_app_teardown_uses_live_registry(app, monkeypatch):
    """Fresh worker processes start with db._Session unset; teardown
    must use the registry rebound by create_app, not a stale import."""
    import overstate_ui.db as dbmod
    from overstate_ui.config import Config
    from overstate_ui.tasks import isolated_app

    # Non-testing boots refuse the placeholder SECRET_KEY.
    monkeypatch.setattr(Config, "SECRET_KEY", "test-worker-key")
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


def test_fleet_keys_parses_actives_and_raises(app):
    """Master-local probe: key counts plus JID-shaped active jobs. Never
    fans out, so it stays instant with any number of dead minions."""
    from overstate_ui.tasks import fleet_keys_now

    class ActiveClient(StubClient):
        def runner(self, fun, **kwargs):
            if fun == "jobs.active":
                return [
                    {"111": {"fun": "test.ping"}, "222": {"fun": "state.highstate"}}
                ]
            return super().runner(fun, **kwargs)

    with app.app_context():
        out = fleet_keys_now(ActiveClient())
    assert out["accepted"] == 1 and out["pending"] == 1
    assert out["active_jids"] == ["111", "222"] and out["active_live"] is True

    class OddClient(StubClient):
        def runner(self, fun, **kwargs):
            return [{"up": ["a"], "down": []}]  # not a jobs.active payload

    with app.app_context():
        bad_shape = fleet_keys_now(OddClient())
    assert bad_shape["active_live"] is False
    assert bad_shape["active_jids"] == []
    with app.app_context(), pytest.raises(SaltApiError):
        fleet_keys_now(StubClient(deny={"key.list_all"}))
    with app.app_context(), pytest.raises(SaltApiError):
        fleet_keys_now(StubClient(deny={"jobs.active"}))


def test_fleet_presence_and_versions_parse_and_raise(app):
    from overstate_ui.tasks import fleet_presence_now, fleet_versions_now

    class VersionClient(StubClient):
        def runner(self, fun, **kwargs):
            if fun == "manage.versions":
                return [{"Up to date": {"m1": "3006.5"}, "Master": "3008.2"}]
            return super().runner(fun, **kwargs)

    with app.app_context():
        presence = fleet_presence_now(StubClient())
    assert presence == {"reachable": True, "up": 1, "down": 0}
    with app.app_context():
        versions = fleet_versions_now(VersionClient())
    assert versions == {"versions": {"3006.5": 1}}
    with app.app_context(), pytest.raises(SaltApiError):
        fleet_presence_now(StubClient(deny={"manage.status"}))
    with app.app_context(), pytest.raises(SaltApiError):
        fleet_versions_now(StubClient(deny={"manage.versions"}))


def test_refresh_uses_worker_result(monkeypatch, admin):
    monkeypatch.setattr(
        "overstate_ui.tasks.queue_or_none",
        lambda *a, **k: StubJob("finished", {"count": 3}),
    )
    rv = admin.post("/minions/refresh", follow_redirects=True)
    assert "Inventory refreshed: 3 minions." in rv.data.decode()


def test_dashboard_without_worker_shows_snapshot(app, admin):
    from overstate_ui.models import Minion

    with app.app_context():
        get_session().add(Minion(id="m1", grains={}, conformity={}))
        get_session().commit()
    html = admin.get("/").data.decode()
    assert "Capabilities" in html
    assert "Background worker unreachable" in html
    assert "No capability data yet." in html


class FakeRotationStore:
    """Dict-backed stand-in for the Redis rotation cache."""

    def __init__(self):
        self.data = {}

    def ping(self):
        return True

    def get(self, key):
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value

    def set(self, key, value, ex=None):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture()
def rotation_store(monkeypatch):
    import overstate_ui.users as users_mod

    store = FakeRotationStore()
    monkeypatch.setattr(users_mod, "_rotation_store", lambda: store)
    return store


def test_rotation_page_and_verify(monkeypatch, admin, rotation_store):
    html = admin.get("/users/rotation").data.decode()
    assert "Rotate salt-api password" in html
    assert "locks the app out" in html  # danger banner above the steps

    class LoginOk:
        def login(self):
            return None

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginOk())
    rv = admin.post(
        "/users/rotation/verify", data={"password": "new"}, follow_redirects=True
    )
    assert "New eauth password works." in rv.data.decode()

    class LoginDenied:
        def login(self):
            raise SaltApiError("denied")

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginDenied())
    rv = admin.post(
        "/users/rotation/verify", data={"password": "bad"}, follow_redirects=True
    )
    assert "verification failed" in rv.data.decode()


def _rotation_password(client):
    import re

    html = client.get("/users/rotation").data.decode()
    return re.search(
        r'font-mono text-sm bg-base-200 rounded px-3 py-2 mt-2 break-all">([^<]+)<',
        html,
    ).group(1)


def test_rotation_password_stable_until_consumed(monkeypatch, admin, rotation_store):
    assert _rotation_password(admin) == _rotation_password(admin)

    class LoginOk:
        def login(self):
            return None

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginOk())
    # Consumed on verify: the next visit mints a fresh one.
    admin.post("/users/rotation/verify", data={"password": "new"})
    first = _rotation_password(admin)
    admin.post("/users/rotation/verify", data={"password": "new"})
    assert _rotation_password(admin) != first


def test_rotation_regenerate_replaces_password(admin, rotation_store):
    import re

    first = _rotation_password(admin)
    rv = admin.post("/users/rotation/regenerate", follow_redirects=True)
    assert rv.status_code == 200
    assert "discarded" in rv.data.decode()
    second = re.search(
        r'font-mono text-sm bg-base-200 rounded px-3 py-2 mt-2 break-all">([^<]+)<',
        rv.data.decode(),
    ).group(1)
    assert second and second != first


def test_rotation_password_copy_button(admin, rotation_store):
    html = admin.get("/users/rotation").data.decode()
    assert 'data-copy="rotation-password"' in html
    assert 'id="rotation-password"' in html


def test_rotation_password_lives_on_server_not_in_cookie(admin, rotation_store):
    password = _rotation_password(admin)
    assert password
    assert len(rotation_store.data) == 1  # the server cache holds it
    with admin.session_transaction() as session:
        assert "rotation_password" not in session


def test_rotation_verify_pops_server_password(monkeypatch, admin, rotation_store):
    _rotation_password(admin)
    assert len(rotation_store.data) == 1

    class LoginOk:
        def login(self):
            return None

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: LoginOk())
    admin.post("/users/rotation/verify", data={"password": "new"})
    assert rotation_store.data == {}


def test_rotation_refuses_without_cache(admin):
    html = admin.get("/users/rotation").data.decode()
    assert "Rotation needs the cache." in html
    assert 'id="rotation-password"' not in html
    rv = admin.post("/users/rotation/regenerate", follow_redirects=True)
    assert "Rotation needs the cache." in rv.data.decode()


def test_rotation_forbidden_for_operator(app):
    from overstate_ui.models import User

    with app.app_context():
        get_session().add(User(username="op", password_hash="x", role="operator"))
        get_session().commit()
        op_id = get_session().query(User).filter_by(username="op").first().id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(op_id)
    assert client.get("/users/rotation").status_code == 403
    assert client.post("/users/rotation/verify").status_code == 403

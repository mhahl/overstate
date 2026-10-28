"""Worker and capability tests: queue fallback, probes, verify."""

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
        if fun in ("grains.items", "grains.item"):
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


def test_probe_history_door_asks_job_cache(app):
    """D3: the history door is jobs.list_jobs on the master, not a DB
    read — Postgres freshness has its own Returns signal."""
    from overstate_ui.tasks import probe_capabilities

    with app.app_context():
        out = probe_capabilities(StubClient(), ping_target="m1")
    assert out["history_ok"] is True
    with app.app_context():
        denied = probe_capabilities(StubClient(deny={"jobs.list_jobs"}))
    assert denied["history_ok"] is False
    assert denied["runner_ok"] is True  # doors stay independent


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


def test_presence_probes_bound_manage_status_timeout(app):
    """manage.status waits out the master timeout for every missing
    returner; fleet_presence_now and the capability runner door carry
    timeout=1/gather_job_timeout=1 so a dead minion cannot stretch
    them past the HTTP backstop."""
    from overstate_ui.tasks import fleet_presence_now, probe_capabilities

    seen: dict = {}

    class RecordingClient(StubClient):
        def runner(self, fun, **kwargs):
            seen[fun] = kwargs
            return super().runner(fun, **kwargs)

    with app.app_context():
        presence = fleet_presence_now(RecordingClient())
    assert presence == {"reachable": True, "up": 1, "down": 0}
    assert seen["manage.status"].get("timeout") == 1
    assert seen["manage.status"].get("gather_job_timeout") == 1
    with app.app_context():
        probe_capabilities(RecordingClient(), ping_target="m1")
    assert seen["manage.status"].get("timeout") == 1
    assert seen["manage.status"].get("gather_job_timeout") == 1


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
    assert "Capability checks" in html
    assert "Background worker unreachable" in html
    assert "No capability data yet." in html


class MergedPod:
    """Per-pod stub for the merged dashboard task fan-outs."""

    def __init__(self, accepted=(), pending=(), up=(), active=None, versions=None):
        self.accepted = set(accepted)
        self.pending = set(pending)
        self.up = set(up)
        self.active = dict(active or {})
        self.versions = dict(versions or {})

    def wheel(self, fun, **kwargs):
        assert fun == "key.list_all"
        return [
            {
                "data": {
                    "return": {
                        "minions": sorted(self.accepted),
                        "minions_pre": sorted(self.pending),
                        "minions_rejected": [],
                        "minions_denied": [],
                    }
                }
            }
        ]

    def runner(self, fun, **kwargs):
        if fun == "jobs.active":
            return [self.active]
        if fun == "manage.status":
            return [{"up": sorted(self.up), "down": []}]
        if fun == "manage.versions":
            return [self.versions]
        return [{}]


def _no_app_worker(monkeypatch):
    """Run *_task fan-outs without a real worker app: patch out the
    isolated-app builder and the unused build_client, and count only on
    the pod_clients stub."""
    import contextlib

    from overstate_ui import tasks_salt as ts

    monkeypatch.setattr(ts, "isolated_app", lambda: contextlib.nullcontext())
    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: object())


def test_fleet_keys_task_unions_pods(monkeypatch):
    from overstate_ui import tasks_salt as ts
    from overstate_ui.tasks import fleet_keys_task

    _no_app_worker(monkeypatch)
    a = MergedPod(accepted=["web-01"], pending=["new-01"], active={"1": {}})
    b = MergedPod(accepted=["web-02", "web-01"], active={"2": {}})
    monkeypatch.setattr(ts, "pod_clients", lambda default: [("pod-0", a), ("pod-1", b)])

    out = fleet_keys_task()
    assert out["accepted"] == 2  # web-01 appears on both pods: counted once
    assert out["pending"] == 1
    assert out["active_jids"] == ["1", "2"]
    assert out["active_live"] is True


def test_fleet_keys_task_raises_when_all_pods_silent(monkeypatch):
    from overstate_ui import tasks_salt as ts
    from overstate_ui.salt_client import SaltApiError
    from overstate_ui.tasks import fleet_keys_task

    _no_app_worker(monkeypatch)

    class Dead:
        def wheel(self, fun, **kwargs):
            raise SaltApiError("down")

        def runner(self, fun, **kwargs):
            raise SaltApiError("down")

    monkeypatch.setattr(ts, "pod_clients", lambda default: [("pod-0", Dead())])
    try:
        fleet_keys_task()
        raise AssertionError("expected SaltApiError")
    except SaltApiError:
        pass


def test_fleet_presence_task_unions_up_and_counts_down(monkeypatch):
    from overstate_ui import tasks_salt as ts
    from overstate_ui.tasks import fleet_presence_task

    _no_app_worker(monkeypatch)
    a = MergedPod(accepted=["web-01"], up=["web-01"])
    # web-02 attached to pod-1 appears "down" on pod-0's view; the merge
    # must not double count it as down when pod-1 sees it up.
    b = MergedPod(accepted=["web-02"], up=["web-02"])
    monkeypatch.setattr(ts, "pod_clients", lambda default: [("pod-0", a), ("pod-1", b)])

    out = fleet_presence_task()
    assert out["up"] == 2
    assert out["down"] == 0


def test_fleet_versions_task_sums_pods(monkeypatch):
    from overstate_ui import tasks_salt as ts
    from overstate_ui.tasks import fleet_versions_task

    _no_app_worker(monkeypatch)
    a = MergedPod(versions={"3008.2": {"web-01": "3008.2"}})
    b = MergedPod(versions={"3008.2": {"web-02": "3008.2", "db-01": "3008.2"}})
    monkeypatch.setattr(ts, "pod_clients", lambda default: [("pod-0", a), ("pod-1", b)])

    out = fleet_versions_task()
    assert out["versions"] == {"3008.2": 3}

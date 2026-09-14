"""Dashboard truth tests: live version skew and master-active
in-flight counts, with snapshot/DB fallbacks when denied."""

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion
from overstate_ui.salt_client import SaltClient


def _raising_transport() -> httpx.MockTransport:
    """Salt-api transport that fails any call loudly: the dashboard
    request paths must never touch Salt, so any attempt is a bug."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dashboard must not call salt-api")

    return httpx.MockTransport(handler)


def _dashboard_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=_raising_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(
            Minion(id="web-01", key_status="accepted", grains={"saltversion": "3006.5"})
        )
        get_session().add(
            Job(
                jid="12345",
                fun="state.highstate",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().add(
            Job(
                jid="stale-1",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def _fake_queue(monkeypatch):
    """Pretend the RQ worker accepted the dashboard probes."""
    import types

    ids = {
        "salt_overview_task": "o1",
        "fleet_truth_task": "t1",
        "capabilities_task": "c1",
    }

    def fake(func, *args, **kwargs):
        return types.SimpleNamespace(id=ids[func.__name__])

    monkeypatch.setattr("overstate_ui.tasks.queue_or_none", fake)


def test_shell_polls_without_touching_salt(monkeypatch):
    """Core async guarantee: the shell enqueues probes, renders the
    snapshot instantly, and polls — performing zero salt-api calls."""
    _fake_queue(monkeypatch)
    html = _dashboard_client().get("/").data.decode()
    assert "Refreshing live data" in html
    assert 'hx-get="/dashboard/panels?overview=o1&amp;truth=t1&amp;caps=c1"' in html
    assert "3006.5" in html  # snapshot versions paint immediately
    assert ">2<" in html  # DB in-flight count, not live


def test_shell_shows_worker_warning_without_redis():
    """No queue (Redis down in tests) means no panels: snapshot shell
    with a worker warning instead of a salt-api claim we never tested."""
    html = _dashboard_client().get("/").data.decode()
    assert "Background worker unreachable" in html
    assert "Database history only" not in html
    assert ">2<" in html
    assert "3006.5" in html


def test_panels_resolve_to_live_and_stop_polling(monkeypatch):
    import overstate_ui.dashboard as dashboard_mod

    def fake_describe(jid):
        return (
            "ready",
            {
                "o1": {
                    "reachable": True,
                    "accepted": 2,
                    "pending": 0,
                    "up": 2,
                    "down": 0,
                },
                "t1": {
                    "versions": {"3006.9": 2},
                    "versions_live": True,
                    "active_jids": ["12345"],
                    "active_live": True,
                },
                "c1": {
                    "wheel_ok": True,
                    "runner_ok": True,
                    "history_ok": True,
                    "ping_ok": True,
                    "ping_target": "web-01",
                    "error": None,
                },
            }[jid],
        )

    monkeypatch.setattr(dashboard_mod, "describe_job", fake_describe)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?overview=o1&truth=t1&caps=c1")
        .data.decode()
    )
    assert "3006.9" in html and "Live from master" in html
    assert ">1<" in html  # only the master-active JID counts
    assert "Keys" in html  # capability rows rendered from the result
    assert "hx-get" not in html  # polling stopped
    assert "Refreshing live data" not in html


def test_panels_keep_polling_while_waiting(monkeypatch):
    import overstate_ui.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "describe_job", lambda jid: ("waiting", None))
    html = (
        _dashboard_client()
        .get("/dashboard/panels?overview=o1&truth=t1&caps=c1")
        .data.decode()
    )
    assert "hx-get" in html and "Refreshing live data" in html
    assert "3006.5" in html  # snapshot still shown meanwhile


def test_panels_fall_back_to_snapshot_when_gone(monkeypatch):
    import overstate_ui.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "describe_job", lambda jid: ("gone", None))
    html = (
        _dashboard_client()
        .get("/dashboard/panels?overview=o1&truth=t1&caps=c1")
        .data.decode()
    )
    assert "3006.5" in html
    assert "No capability data yet." in html
    assert "hx-get" not in html
    assert "Refreshing live data" not in html


def test_describe_job_never_raises(monkeypatch):
    import redis

    from overstate_ui import tasks_queue

    assert tasks_queue.describe_job(None) == ("gone", None)
    assert tasks_queue.describe_job("") == ("gone", None)

    def boom():
        raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(tasks_queue, "get_redis_client", boom)
    assert tasks_queue.describe_job("abc123") == ("gone", None)


def test_normalize_versions_shapes():
    from overstate_ui.tasks import normalize_versions

    assert normalize_versions({"a": "1", "b": "1", "c": "2"}) == {"1": 2, "2": 1}
    assert normalize_versions(
        {"Up to date": {"a": "1", "b": "1"}, "Master": "3008.2"}
    ) == {"1": 2}
    assert normalize_versions({"Minion offline": {"a": False}}) == {}
    assert normalize_versions({"Up to date": ["a"]}) == {}
    assert normalize_versions("3006") == {}
    assert normalize_versions(None) == {}


def test_non_jid_active_payload_falls_back():
    from overstate_ui.tasks import fleet_truth_now

    class OddClient:
        def runner(self, fun, **kwargs):
            return [{"up": ["a"], "down": []}]

    out = fleet_truth_now(OddClient())
    assert out["active_live"] is False
    assert out["active_jids"] == []

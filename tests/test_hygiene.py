"""Hygiene package (P5): presence cache, sync logging, docs, seed guard."""

import logging
import pathlib

import pytest

from overstate_ui import create_app, tasks_queue
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.dashboard import snapshot_versions
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion

REPO = pathlib.Path(__file__).resolve().parent.parent


class CountingClient:
    """Stub salt-api client counting round-trips for the cache test."""

    def __init__(self):
        self.wheel_calls = 0
        self.runner_calls = 0

    def wheel(self, fun, **kwargs):
        self.wheel_calls += 1
        return [{"data": {"return": {"minions": ["web-01"], "minions_pre": []}}}]

    def runner(self, fun, **kwargs):
        self.runner_calls += 1
        return [{"up": ["web-01"], "down": []}]


class FakeRedis:
    """Minimal Redis surface for the presence cache (bytes on get)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode() if isinstance(value, str) else value


def _app(salt_client=None):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    if salt_client is not None:
        app.extensions["salt_client"] = salt_client
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


def _login(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


def test_presence_served_from_cache_within_ttl(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(tasks_queue, "get_redis_client", lambda: fake)
    stub = CountingClient()
    client = _login(_app(stub))
    first = client.get("/minions/presence")
    assert first.status_code == 200
    assert first.get_json() == {"web-01": "up"}
    assert stub.wheel_calls == 1
    second = client.get("/minions/presence")
    assert second.get_json() == {"web-01": "up"}
    assert stub.wheel_calls == 1
    assert stub.runner_calls == 1


def test_presence_falls_back_without_redis(monkeypatch):
    import redis

    def _down():
        raise redis.exceptions.ConnectionError("redis down")

    monkeypatch.setattr(tasks_queue, "get_redis_client", _down)
    stub = CountingClient()
    client = _login(_app(stub))
    rv = client.get("/minions/presence")
    assert rv.status_code == 200
    assert rv.get_json() == {"web-01": "up"}
    assert stub.wheel_calls == 1


def test_snapshot_versions_counts_grain_cache():
    app = _app()
    with app.app_context():
        session = get_session()
        session.add(
            Minion(id="m1", key_status="accepted", grains={"saltversion": "3008.2"})
        )
        session.add(
            Minion(id="m2", key_status="accepted", grains={"saltversion": "3008.2"})
        )
        session.add(Minion(id="m3", key_status="accepted", grains={}))
        session.commit()
        assert snapshot_versions() == {"3008.2": 2}


def test_index_sync_failure_logged(monkeypatch, caplog):
    app = _app()
    with app.app_context():
        get_session().add(
            Job(jid="123", fun="test.ping", tgt="*", tgt_type="glob", user="admin")
        )
        get_session().commit()
    monkeypatch.setattr(
        "overstate_ui.jobs.sync_job",
        lambda jid: (_ for _ in ()).throw(ValueError("stale")),
    )
    client = _login(app)
    with caplog.at_level(logging.WARNING, logger="overstate_ui.jobs"):
        assert client.get("/jobs/").status_code == 200
    assert "sync_job(123) failed" in caplog.text


def test_deployment_docs_match_writable_app_mount():
    text = (REPO / "docs" / "install-kubernetes.md").read_text()
    assert "srv-data" in text
    assert "masters read-only" in text


def test_seed_admin_refuses_prod_without_password(monkeypatch):
    import overstate_ui.db as dbmod

    monkeypatch.setenv("OVERSTATE_ENV", "prod")
    monkeypatch.delenv("TLS_CERT", raising=False)
    app = create_app(TestConfig)
    with app.app_context():
        dbmod.create_all()
        with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
            seed_admin()
        assert seed_admin(password="explicit") is True
    dbmod._Session.remove()


def test_seed_admin_refuses_tls_without_password(monkeypatch):
    import overstate_ui.db as dbmod

    monkeypatch.delenv("OVERSTATE_ENV", raising=False)
    monkeypatch.setenv("TLS_CERT", "/srv/tls/app.crt")
    app = create_app(TestConfig)
    with app.app_context():
        dbmod.create_all()
        with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
            seed_admin()
    dbmod._Session.remove()


def test_seed_admin_dev_still_prints(monkeypatch, capsys):
    import overstate_ui.db as dbmod

    monkeypatch.delenv("OVERSTATE_ENV", raising=False)
    monkeypatch.delenv("TLS_CERT", raising=False)
    app = create_app(TestConfig)
    with app.app_context():
        dbmod.create_all()
        assert seed_admin() is True
    dbmod._Session.remove()
    assert "seeded admin" in capsys.readouterr().out

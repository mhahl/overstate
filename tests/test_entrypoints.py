"""Entrypoint tests: wsgi retry init and the RQ worker main."""

import sys

import pytest
from sqlalchemy.exc import OperationalError


class FakeAppCtx:
    def __init__(self, app):
        self._app = app

    def __enter__(self):
        return self._app

    def __exit__(self, *exc):
        return False


class FakeApp:
    def app_context(self):
        return FakeAppCtx(self)


def load_wsgi(monkeypatch, create_all, seed_admin):
    """Import overstate_ui.wsgi with side effects stubbed.

    Module import builds the app and runs init_with_retry, so the
    collaborators are patched before the first import in each test.
    Returns (module, sleeps).
    """
    sleeps = []
    sys.modules.pop("overstate_ui.wsgi", None)
    import overstate_ui

    if hasattr(overstate_ui, "wsgi"):
        # sys.modules.pop alone leaves the package attribute behind and
        # the import below would return the stale module object.
        delattr(overstate_ui, "wsgi")
    monkeypatch.setattr("overstate_ui.create_app", lambda *a, **k: FakeApp())
    monkeypatch.setattr("overstate_ui.db.create_all", create_all)
    monkeypatch.setattr("overstate_ui.auth.seed_admin", seed_admin)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    from overstate_ui import wsgi

    return wsgi, sleeps


def _boom(*a, **k):
    raise OperationalError("down", None, None)


def test_wsgi_succeeds_first_try(monkeypatch):
    calls = []
    wsgi, sleeps = load_wsgi(
        monkeypatch,
        create_all=lambda: calls.append("create"),
        seed_admin=lambda password=None: calls.append(password),
    )
    assert isinstance(wsgi.app, FakeApp)
    assert calls == ["create", None]
    assert sleeps == []


def test_wsgi_retries_then_recovers(monkeypatch):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            _boom()

    wsgi, sleeps = load_wsgi(
        monkeypatch,
        create_all=flaky,
        seed_admin=lambda password=None: None,
    )
    assert len(attempts) == 3
    assert sleeps == [wsgi.RETRY_DELAY_SECONDS] * 2


def test_wsgi_exhausts_attempts_and_raises(monkeypatch):
    wsgi, sleeps = load_wsgi(
        monkeypatch,
        create_all=lambda: None,
        seed_admin=lambda password=None: None,
    )
    assert sleeps == []
    monkeypatch.setattr(wsgi, "create_all", _boom)
    with pytest.raises(OperationalError):
        wsgi.init_with_retry(FakeApp(), attempts=3, delay=5)
    assert sleeps == [5, 5]  # no sleep after the final attempt


def test_worker_main_serves_salt_queue(monkeypatch):
    import overstate_ui.worker as worker_mod
    from overstate_ui.tasks import QUEUE_NAME

    seen = {}

    class FakeConn:
        pass

    conn = FakeConn()

    def fake_from_url(url):
        seen["url"] = url
        return conn

    class FakeQueue:
        def __init__(self, name, connection=None):
            seen["queue"] = name
            seen["connection"] = connection

    class FakeWorker:
        def __init__(self, queues, connection=None):
            seen["queues"] = queues
            seen["worker_conn"] = connection

        def work(self):
            seen["worked"] = True

    monkeypatch.setattr("redis.from_url", fake_from_url)
    monkeypatch.setattr("rq.Queue", FakeQueue)
    monkeypatch.setattr("rq.Worker", FakeWorker)
    worker_mod.main()
    assert seen["queue"] == QUEUE_NAME
    assert len(seen["queues"]) == 1
    assert seen["worked"] is True
    assert seen["connection"] is conn
    assert seen["worker_conn"] is conn

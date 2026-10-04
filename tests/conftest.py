"""Shared fixtures. The login rate limiter is process-global and keyed by
IP, and every test client posts from 127.0.0.1 — clear it per test so
tests stay isolated from each other's login volume."""

import pytest

from overstate_ui import auth


@pytest.fixture(autouse=True)
def _clear_login_attempts():
    auth._attempts.clear()
    yield
    auth._attempts.clear()


class FakeRedis:
    """Dict-backed stand-in for the redis calls the cache helpers use.

    There is no redis server in test runs, so cache behavior is pinned
    through this instead of the real client.
    """

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


@pytest.fixture()
def fake_redis(monkeypatch):
    """Route tasks_queue's redis client at a FakeRedis; returns it."""
    import overstate_ui.tasks_queue as queue_mod

    fake = FakeRedis()
    monkeypatch.setattr(queue_mod, "get_redis_client", lambda: fake)
    return fake

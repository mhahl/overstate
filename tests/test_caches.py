"""Process-level caches: redis clients, pod URLs/clients, roster payloads.

The web paths avoid repeat Salt/K8s/Redis setup costs with small
in-process and short-TTL caches. These tests pin the caching behavior
itself (reuse, TTL expiry, stale fallback, outage discipline) with a
dict-backed FakeRedis — there is no redis server in test runs.
"""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    return app


@pytest.fixture(autouse=True)
def _drop_caches():
    from overstate_ui import fleet, tasks_queue

    fleet.drop_client_cache()
    tasks_queue.drop_redis_clients()
    yield
    fleet.drop_client_cache()
    tasks_queue.drop_redis_clients()


def test_minion_is_up_tristate(monkeypatch, fake_redis):
    import overstate_ui.minions_helpers as helpers_mod
    from overstate_ui.minions_helpers import minion_is_up
    from overstate_ui.tasks_queue import write_roster_cache

    write_roster_cache({"web-01": "accepted", "down-01": "accepted"}, {"web-01"})
    assert minion_is_up(object(), "web-01") is True
    assert minion_is_up(object(), "down-01") is False
    assert minion_is_up(object(), "unknown-01") is False
    # Unreachable roster fails open: None means "keep the live call".
    monkeypatch.setattr(
        helpers_mod, "cached_roster", lambda client, **kwargs: ({}, set(), False)
    )
    assert minion_is_up(object(), "web-01") is None


def test_redis_client_reused_per_url(app):
    from overstate_ui.tasks_queue import drop_redis_clients, get_redis_client

    with app.app_context():
        assert get_redis_client() is get_redis_client()
        drop_redis_clients()
        assert get_redis_client() is get_redis_client()


class _FakeK8sConfig:
    available = True
    namespace = "salt"


class _FakeK8s:
    """K8sClient double: counts API calls, fails on demand."""

    def __init__(self, replicas=2, fail=False):
        self.calls = 0
        self.replicas = replicas
        self.fail = fail
        self.config = _FakeK8sConfig()

    def statefulset_rollout(self, name):
        from overstate_ui.k8s import K8sError

        self.calls += 1
        if self.fail:
            raise K8sError("api down")
        assert name == "salt-master"
        return {"replicas": self.replicas}


def _patch_k8s(monkeypatch, fake):
    import overstate_ui.fleet as fleet_mod

    monkeypatch.setattr(fleet_mod, "K8sClient", lambda *a, **k: fake)
    return fake


def test_pod_urls_cached_within_ttl(app, monkeypatch):
    import overstate_ui.fleet as fleet_mod

    k8s = _patch_k8s(monkeypatch, _FakeK8s(replicas=2))
    with app.app_context():
        first = fleet_mod.pod_api_urls()
        second = fleet_mod.pod_api_urls()
    assert first == second
    assert len(first) == 2
    assert k8s.calls == 1


def test_pod_urls_refetch_after_ttl(app, monkeypatch):
    import overstate_ui.fleet as fleet_mod

    k8s = _patch_k8s(monkeypatch, _FakeK8s(replicas=2))
    with app.app_context():
        fleet_mod.pod_api_urls()
        fleet_mod._pod_urls_cache["at"] -= fleet_mod.POD_URLS_TTL + 1
        fleet_mod.pod_api_urls()
    assert k8s.calls == 2


def test_pod_urls_fall_back_to_stale_on_k8s_error(app, monkeypatch):
    import overstate_ui.fleet as fleet_mod

    k8s = _patch_k8s(monkeypatch, _FakeK8s(replicas=2))
    with app.app_context():
        warm = fleet_mod.pod_api_urls()
        k8s.fail = True
        # TTL expiry forces a refetch attempt, which fails: the
        # last-known list is reused instead of collapsing to [].
        fleet_mod._pod_urls_cache["at"] -= fleet_mod.POD_URLS_TTL + 1
        assert fleet_mod.pod_api_urls() == warm


def test_pod_clients_reused_across_calls(app, monkeypatch):
    import overstate_ui.fleet as fleet_mod

    _patch_k8s(monkeypatch, _FakeK8s(replicas=2))
    with app.app_context():
        first = fleet_mod.pod_clients(object())
        second = fleet_mod.pod_clients(object())
    assert [name for name, _ in first] == ["pod-0", "pod-1"]
    assert first[0][1] is second[0][1]
    assert first[1][1] is second[1][1]


def test_roster_cache_roundtrip(fake_redis):
    from overstate_ui.tasks_queue import (
        ROSTER_CACHE_KEY,
        read_roster_cache,
        write_roster_cache,
    )

    assert read_roster_cache() is None
    write_roster_cache({"web-01": "accepted"}, {"web-01"})
    assert fake_redis.store[ROSTER_CACHE_KEY]
    assert read_roster_cache() == {
        "statuses": {"web-01": "accepted"},
        "up": ["web-01"],
    }


def test_clear_key_caches_drops_both(fake_redis):
    from overstate_ui.tasks_queue import (
        KEYS_CACHE_KEY,
        ROSTER_CACHE_KEY,
        clear_key_caches,
        write_keys_cache,
        write_roster_cache,
    )

    write_roster_cache({"web-01": "accepted"}, set())
    write_keys_cache({"accepted": []}, [])
    assert ROSTER_CACHE_KEY in fake_redis.store
    assert KEYS_CACHE_KEY in fake_redis.store
    clear_key_caches()
    assert fake_redis.store == {}


def test_cached_roster_serves_warm_cache_without_salt(monkeypatch, fake_redis):
    import overstate_ui.minions_helpers as helpers_mod
    from overstate_ui.minions_helpers import cached_roster
    from overstate_ui.tasks_queue import write_roster_cache

    write_roster_cache({"web-01": "accepted"}, {"web-01"})

    def _boom(client, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("Salt must not be touched on a cache hit")

    monkeypatch.setattr(helpers_mod, "live_roster", _boom)
    statuses, up, reachable = cached_roster(object())
    assert (statuses, up, reachable) == ({"web-01": "accepted"}, {"web-01"}, True)


def test_cached_roster_never_caches_outages(monkeypatch, fake_redis):
    import overstate_ui.minions_helpers as helpers_mod
    from overstate_ui.minions_helpers import cached_roster
    from overstate_ui.tasks_queue import ROSTER_CACHE_KEY

    monkeypatch.setattr(
        helpers_mod, "live_roster", lambda client, **kwargs: ({}, set(), False)
    )
    assert cached_roster(object()) == ({}, set(), False)
    assert ROSTER_CACHE_KEY not in fake_redis.store


def test_cached_roster_writes_through_when_reachable(monkeypatch, fake_redis):
    import overstate_ui.minions_helpers as helpers_mod
    from overstate_ui.minions_helpers import cached_roster
    from overstate_ui.tasks_queue import read_roster_cache

    monkeypatch.setattr(
        helpers_mod,
        "live_roster",
        lambda client, **kwargs: ({"web-01": "accepted"}, {"web-01"}, True),
    )
    assert cached_roster(object()) == ({"web-01": "accepted"}, {"web-01"}, True)
    assert read_roster_cache() == {
        "statuses": {"web-01": "accepted"},
        "up": ["web-01"],
    }

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
        "fleet_keys_task": "k1",
        "fleet_presence_task": "p1",
        "fleet_versions_task": "v1",
        "capabilities_task": "c1",
        "master_status_task": "m1",
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
    assert (
        'hx-get="/dashboard/panels?keys=k1&amp;presence=p1&amp;versions=v1&amp;caps=c1&amp;masters=m1&amp;seen='
        in html
    )  # shell seeds the fingerprint so an unchanged first poll is a 204
    assert "&amp;started=" in html  # poll clock for the stale-probe cutoff
    assert 'hx-trigger="every 2s"' in html  # no load trigger: it refires on every swap
    assert html.index("Refreshing live data") < html.index("Live events")  # header slot
    # Reachability is still unknown while probes are in flight: no
    # "unreachable" banner until polling settles (else it flashes on
    # every load and vanishes seconds later).
    assert "Database history only" not in html
    assert "3006.5" in html  # snapshot versions paint immediately
    assert ">2<" in html  # DB in-flight count, not live
    assert "Salt masters" in html and "Probing masters" in html


def test_shell_queues_capabilities_without_minions(monkeypatch):
    """Wheel/runner/history doors must probe before any minion is
    inventoried: gating the caps probe on a ping target leaves @wheel
    and @runner failing with no capabilities on a fresh deploy."""
    _fake_queue(monkeypatch)
    client = _dashboard_client()
    with client.application.app_context():
        get_session().query(Minion).delete()
        get_session().commit()
    html = client.get("/").data.decode()
    assert "caps=c1" in html


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
                "k1": {
                    "reachable": True,
                    "accepted": 2,
                    "pending": 3,
                    "active_jids": ["12345"],
                    "active_live": True,
                },
                "p1": {"reachable": True, "up": 2, "down": 0},
                "v1": {"versions": {"3006.9": 2}},
                "c1": {
                    "wheel_ok": True,
                    "runner_ok": True,
                    "history_ok": True,
                    "ping_ok": True,
                    "ping_target": "web-01",
                    "error": None,
                },
                "m1": {
                    "available": True,
                    "sts": "salt-master",
                    "complete": True,
                    "replicas": 2,
                    "ready_replicas": 2,
                    "updated_replicas": 2,
                    "ready_pods": 2,
                    "pods": [
                        {
                            "name": "salt-master-0",
                            "phase": "Running",
                            "ready": True,
                            "image": "quay.io/sigaint/overstate-salt-master:lts-pg1",
                            "image_short": "overstate-salt-master:lts-pg1",
                            "restarts": 0,
                        },
                        {
                            "name": "salt-master-1",
                            "phase": "Running",
                            "ready": True,
                            "image": "quay.io/sigaint/overstate-salt-master:lts-pg1",
                            "image_short": "overstate-salt-master:lts-pg1",
                            "restarts": 3,
                        },
                    ],
                    "images_differ": False,
                    "config_revision": "4242",
                },
            }[jid],
        )

    monkeypatch.setattr(dashboard_mod, "describe_job", fake_describe)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1")
        .data.decode()
    )
    assert "3006.9" in html and "Live from master" in html
    assert ">1<" in html  # only the master-active JID counts
    assert ">3<" in html  # live pending keys, not snapshot zero
    assert "2 / 0" in html  # live up/down, not snapshot dashes
    assert "Keys" in html  # capability rows rendered from the result
    assert "salt-master-0" in html and "salt-master-1" in html
    assert "Rollout complete" in html and "2/2 updated" in html
    assert "overstate-salt-master:lts-pg1" in html
    assert "3× restarted" in html and "4242" in html
    assert "Master Settings" in html  # admin sees the settings link
    assert "hx-get" not in html  # polling stopped
    assert "opacity-60 invisible" in html  # spinner slot reserved, buttons unmoved


def test_panels_hydrate_fast_panels_while_fanout_waits(monkeypatch):
    """Dead-minion regression: master-local panels (keys) paint live
    while fan-out panels (presence) are still waiting — one down minion
    must no longer blank the whole dashboard."""
    import overstate_ui.dashboard as dashboard_mod

    states = {
        "k1": (
            "ready",
            {
                "reachable": True,
                "accepted": 2,
                "pending": 3,
                "active_jids": [],
                "active_live": True,
            },
        ),
        "p1": ("waiting", None),
        "v1": ("waiting", None),
        "c1": ("gone", None),
        "m1": ("waiting", None),
    }
    monkeypatch.setattr(dashboard_mod, "describe_job", states.get)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1")
        .data.decode()
    )
    assert ">2<" in html and ">3<" in html  # keys live despite dead minion
    assert ">–<" in html  # up/down stay snapshot dashes while presence waits
    assert "3006.5" in html  # versions still snapshot meanwhile
    assert "Probing capabilities" in html  # caps job gone, presence still waiting
    assert "Probing masters" in html  # masters still waiting too
    assert "Refreshing live data" in html and "hx-get" in html  # keeps polling


def test_panels_skip_unchanged_renders_while_waiting(monkeypatch):
    """No 204 churn: while nothing new is ready the poll answers 204 so
    htmx swaps nothing and the probing spinner keeps spinning instead
    of restarting on an identical re-render."""
    import re

    import overstate_ui.dashboard as dashboard_mod

    states = {
        "k1": ("waiting", None),
        "p1": ("waiting", None),
        "v1": ("waiting", None),
        "c1": ("waiting", None),
        "m1": ("waiting", None),
    }
    monkeypatch.setattr(dashboard_mod, "describe_job", states.get)
    client = _dashboard_client()
    base = "/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1"
    html = client.get(base).data.decode()
    assert "Refreshing live data" in html
    assert "invisible" not in html  # spinner slot visible while probing
    seen = re.search(r"seen=([^\"&]+)", html).group(1)

    # Unchanged re-poll: 204, so htmx swaps nothing and the spinner
    # keeps spinning instead of restarting on an identical render.
    rv = client.get(f"{base}&seen={seen}")
    assert rv.status_code == 204
    assert rv.data == b""

    # One panel resolves: full re-render carrying the new fingerprint...
    states["k1"] = ("ready", {"reachable": True, "accepted": 2})
    html = client.get(f"{base}&seen={seen}").data.decode()
    assert "Refreshing live data" in html
    assert "invisible" not in html  # still visible: presence still waiting
    new_seen = re.search(r"seen=([^\"&]+)", html).group(1)
    assert new_seen != seen

    # ...and a re-poll with that fingerprint is a 204 again.
    rv = client.get(f"{base}&seen={new_seen}")
    assert rv.status_code == 204
    assert rv.data == b""


def test_panels_fall_back_to_snapshot_when_gone(monkeypatch):
    import overstate_ui.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "describe_job", lambda jid: ("gone", None))
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1")
        .data.decode()
    )
    assert "3006.5" in html
    assert "No capability data yet." in html
    assert "Master status unavailable" in html
    assert "hx-get" not in html
    assert "opacity-60 invisible" in html  # slot reserved, buttons unmoved
    # Polling settled with nothing live: reachability resolved False,
    # so the banner legitimately appears here (unlike the probing shell).
    assert "Database history only" in html


def test_panels_give_up_after_stale_cutoff(monkeypatch):
    """Worker died mid-probe: a poll older than the cutoff renders its
    final state — banner gone, polling stopped, snapshot fallbacks."""
    import overstate_ui.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "describe_job", lambda jid: ("waiting", None))
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&started=1")
        .data.decode()
    )
    assert "opacity-60 invisible" in html  # slot reserved, buttons unmoved
    assert "hx-get" not in html
    assert "3006.5" in html  # snapshot versions remain
    assert "No capability data yet." in html


def test_expired_poll_keeps_resolved_results(monkeypatch):
    """Give-up keeps whatever did resolve instead of blanking to pure
    snapshot: live overview paints, the rest falls back, polling stops."""
    import overstate_ui.dashboard as dashboard_mod

    def fake_describe(jid):
        if jid == "k1":
            return (
                "ready",
                {
                    "reachable": True,
                    "accepted": 2,
                    "pending": 5,
                    "active_jids": [],
                    "active_live": True,
                },
            )
        return ("waiting", None)

    monkeypatch.setattr(dashboard_mod, "describe_job", fake_describe)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&started=1")
        .data.decode()
    )
    assert ">5<" in html  # live pending count, not snapshot zero
    assert "opacity-60 invisible" in html  # slot reserved, buttons unmoved
    assert "hx-get" not in html


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
    from overstate_ui.tasks import fleet_keys_now

    class OddClient:
        def wheel(self, fun, **kwargs):
            return [{"data": {"return": {"minions": ["m1"], "minions_pre": []}}}]

        def runner(self, fun, **kwargs):
            return [{"up": ["a"], "down": []}]

    out = fleet_keys_now(OddClient())
    assert out["active_live"] is False
    assert out["active_jids"] == []


def _masters_payload(**over):
    payload = {
        "available": True,
        "sts": "salt-master",
        "complete": True,
        "replicas": 2,
        "ready_replicas": 2,
        "updated_replicas": 2,
        "ready_pods": 2,
        "pods": [
            {
                "name": "salt-master-0",
                "phase": "Running",
                "ready": True,
                "image": "quay.io/sigaint/overstate-salt-master:lts-pg1",
                "image_short": "overstate-salt-master:lts-pg1",
                "restarts": 0,
            },
            {
                "name": "salt-master-1",
                "phase": "Running",
                "ready": True,
                "image": "quay.io/sigaint/overstate-salt-master:lts-pg1",
                "image_short": "overstate-salt-master:lts-pg1",
                "restarts": 0,
            },
        ],
        "images_differ": False,
        "config_revision": "4242",
    }
    payload.update(over)
    return payload


def test_panels_masters_unavailable_off_cluster(monkeypatch):
    """The probe reported no cluster connection: the panel says so
    immediately instead of spinning, while other panels keep polling."""
    import overstate_ui.dashboard as dashboard_mod

    def fake_describe(jid):
        if jid == "m1":
            return ("ready", {"available": False})
        return ("waiting", None)

    monkeypatch.setattr(dashboard_mod, "describe_job", fake_describe)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1")
        .data.decode()
    )
    assert "Master status unavailable" in html
    assert "Probing masters" not in html
    assert "hx-get" in html  # other panels still outstanding


def test_panels_masters_progressing_rollout(monkeypatch):
    """A mid-rollout master pair paints per-pod state and the image
    drift warning instead of a bare ready count."""
    import overstate_ui.dashboard as dashboard_mod

    pods = _masters_payload()["pods"]
    pods[1] = {
        "name": "salt-master-1",
        "phase": "Pending",
        "ready": False,
        "image": "quay.io/sigaint/overstate-salt-master:lts-pg2",
        "image_short": "overstate-salt-master:lts-pg2",
        "restarts": 0,
    }
    payload = _masters_payload(
        complete=False,
        ready_replicas=1,
        updated_replicas=1,
        ready_pods=1,
        pods=pods,
        images_differ=True,
        config_revision=None,
    )

    def fake_describe(jid):
        if jid == "m1":
            return ("ready", payload)
        return ("gone", None)

    monkeypatch.setattr(dashboard_mod, "describe_job", fake_describe)
    html = (
        _dashboard_client()
        .get("/dashboard/panels?keys=k1&presence=p1&versions=v1&caps=c1&masters=m1")
        .data.decode()
    )
    assert "rollout progressing" in html
    assert "1/2 updated" in html
    assert "pending" in html  # unready pod shows its phase
    assert "different images" in html
    assert "unknown" in html  # no config revision yet

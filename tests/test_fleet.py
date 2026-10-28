"""v2 unit 3 tests: fleet presets prefill, review-modal gating, launch flow."""

import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import DESTRUCTIVE_FUNS
from overstate_ui.models import Job
from overstate_ui.salt_client import SaltClient


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "local_async":
            return httpx.Response(
                200, json={"return": [{"jid": "42424", "minions": ["m1"]}]}
            )
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_empty_target_never_fires(client):
    from overstate_ui.db import get_session as _session

    with client.app.app_context():
        before = _session().query(Job).count()
    rv = client.post(
        "/jobs/run",
        data={"tgt": "", "tgt_type": "glob", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 302
    with client.app.app_context():
        assert _session().query(Job).count() == before
    assert "never fires" in client.get(rv.headers["Location"]).data.decode()


def test_blank_bulk_param_prefills_no_target(client):
    html = client.get("/jobs/new?bulk=").data.decode()
    assert "Target prefilled as" not in html


def test_run_validation_failure_preserves_input(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "",
            "mode": "sync",
            "via": "ssh",
            "save_as": "recall",
            "batch_mode": "count",
            "batch_size": "5",
            "stop_after": "2",
        },
    )
    assert rv.status_code == 302
    html = client.get(rv.headers["Location"]).data.decode()
    assert 'value="web-*"' in html
    assert '<option value="sync" selected>' in html
    assert '<option value="ssh" selected>' in html
    assert 'value="recall"' in html
    assert '<option value="count" selected>' in html
    assert 'value="5"' in html


def test_fun_picker_keyboard_hooks_present(client):
    html = client.get("/jobs/new").data.decode()
    assert "aria-activedescendant" in html
    assert "ArrowDown" in html
    assert 'id="fun-list"' in html


def test_confirm_cancel_carries_state(client):
    from flask import render_template

    with client.app.test_request_context("/"):
        html = render_template(
            "job_confirm.html",
            tgt="web-*",
            tgt_type="glob",
            fun="state.highstate",
            raw_args="",
            mode="sync",
            via="local",
            save_as="",
            batch_mode="off",
            batch_size=25,
            stop_after=1,
            matched=[],
            preview=None,
            preview_minion=None,
            preview_note=None,
        )
    assert "tgt=web-*" in html
    assert "mode=sync" in html
    assert "fun=state.highstate" in html


def test_fleet_presets_prefill(client):
    rv = client.get("/jobs/new", query_string={"preset": "service-restart"})
    assert rv.status_code == 200
    assert b"service.restart" in rv.data
    rv = client.get("/jobs/new", query_string={"preset": "process-signal"})
    assert b"ps.kill_pid" in rv.data


def test_destructive_without_confirm_shows_review(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "service.restart",
            "args": "nginx",
            "mode": "async",
        },
    )
    assert rv.status_code == 200
    assert b"Review" in rv.data
    assert b"Type <code>web-*</code> to confirm" in rv.data
    assert b'name="confirm_tgt"' in rv.data
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_destructive_with_unconfirmed_value_stays(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "service.restart",
            "args": "nginx",
            "mode": "async",
            "confirmed": "no",
        },
    )
    assert rv.status_code == 200
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_destructive_with_confirmed_but_untyped_stays(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "service.restart",
            "args": "nginx",
            "mode": "async",
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "Type the target exactly" in rv.data.decode()
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_destructive_with_confirmed_launches(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "service.restart",
            "args": "nginx",
            "mode": "async",
            "confirmed": "yes",
            "confirm_tgt": "web-*",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert len(jid) == 20 and jid.isdigit()  # app-side shared JID (D12)
    with client.app.app_context():
        job = get_session().get(Job, jid)
        assert job is not None and job.fun == "service.restart"


def test_review_modal_lists_match_count(client):
    from overstate_ui.models import Minion

    with client.app.app_context():
        get_session().add(Minion(id="web-01", grains={}, conformity={}))
        get_session().add(Minion(id="web-02", grains={}, conformity={}))
        get_session().commit()
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "service.restart",
            "args": "nginx",
            "mode": "async",
        },
    )
    html = rv.data.decode()
    assert "2 minions" in html
    assert "web-01" in html and "web-02" in html


def test_state_apply_requires_review(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.apply",
            "args": "",
            "mode": "async",
        },
    )
    assert rv.status_code == 200
    assert b"Review" in rv.data
    with client.app.app_context():
        assert get_session().query(Job).count() == 0


def test_state_highstate_dry_run_skips_review(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.highstate",
            "args": "test=True",
            "mode": "async",
        },
    )
    assert rv.status_code == 302


def test_non_destructive_needs_no_confirm(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "async",
        },
    )
    assert rv.status_code == 302


def test_destructive_set_covers_package_changes():
    from overstate_ui.jobs import CONFIRM_FUNS

    assert {"pkg.install", "pkg.remove"} <= set(DESTRUCTIVE_FUNS)
    assert {"state.apply", "state.highstate", "pkg.install", "service.restart"} <= set(
        CONFIRM_FUNS
    )


class RecordingPod:
    """Named pod stub recording runner and local calls."""

    def __init__(self, name, up=(), payloads=None):
        self.name = name
        self.up = set(up)
        self.payloads = dict(payloads or {})
        self.runners = []
        self.locals = []

    def runner(self, fun, **kwargs):
        self.runners.append((fun, kwargs))
        if fun == "manage.status":
            return [{"up": sorted(self.up), "down": []}]
        return [{}]

    def local(self, tgt, fun, **kwargs):
        self.locals.append((tgt, fun, kwargs))
        return [{tgt: self.payloads.get(fun, {"return": "ok"})}]


def test_fleet_pod_map_routes_local_one_to_owning_pod(monkeypatch):
    import overstate_ui.fleet as fleet_mod
    import overstate_ui.tasks_queue as tq_mod

    pod_a = RecordingPod("pod-0", up={"web-01"})
    pod_b = RecordingPod("pod-1", up={"web-02"})
    default = RecordingPod("master")
    monkeypatch.setattr(fleet_mod, "pod_clients", lambda default: [("pod-0", pod_a), ("pod-1", pod_b)])
    monkeypatch.setattr(tq_mod, "read_pod_map_cache", lambda: None)
    monkeypatch.setattr(tq_mod, "write_pod_map_cache", lambda payload: None)

    mapping = fleet_mod.fleet_pod_map(default)
    assert mapping == {"web-01": "pod-0", "web-02": "pod-1"}

    # web-02 is attached to pod-1: the read must hit pod-1, not the VIP.
    out = fleet_mod.local_one(default, "web-02", "mine.get", arg=["web-02", "x"])
    assert pod_b.locals and pod_b.locals[0][1] == "mine.get"
    assert pod_a.locals == []
    assert out == {"return": "ok"}


def test_fleet_pod_map_empty_falls_back_to_default(monkeypatch):
    import overstate_ui.fleet as fleet_mod
    import overstate_ui.tasks_queue as tq_mod

    pod_a = RecordingPod("pod-0", up={"web-01"})
    default = RecordingPod("master", payloads={"test.ping": "pong"})
    # web-02 is not up anywhere: local_one must fall back to the default
    # client (single-master behavior) instead of guessing a pod.
    monkeypatch.setattr(fleet_mod, "pod_clients", lambda default: [("pod-0", pod_a)])
    monkeypatch.setattr(tq_mod, "read_pod_map_cache", lambda: None)
    monkeypatch.setattr(tq_mod, "write_pod_map_cache", lambda payload: None)

    out = fleet_mod.local_one(default, "web-02", "test.ping")
    assert out == "pong"
    assert pod_a.locals == []


def test_kill_fans_out_to_all_pods(monkeypatch, client):
    import overstate_ui.jobs as jobs_mod

    pod_a = RecordingPod("pod-0")
    pod_b = RecordingPod("pod-1")
    monkeypatch.setattr(jobs_mod, "pod_clients", lambda default: [("pod-0", pod_a), ("pod-1", pod_b)])
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20261028000000000001",
                fun="state.apply",
                tgt="web-*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.commit()
    rv = client.post("/jobs/20261028000000000001/kill", follow_redirects=True)
    assert rv.status_code == 200
    # Both pods got the kill publish under the SAME shared kill JID.
    assert len(pod_a.locals) == 1 and len(pod_b.locals) == 1
    (ta, fa, ka), = pod_a.locals
    (tb, fb, kb), = pod_b.locals
    assert fa == "saltutil.kill_job" and fb == "saltutil.kill_job"
    assert ka["arg"] == ["20261028000000000001"]
    assert ka["asynchronous"] is True and kb["asynchronous"] is True
    assert ka["jid"] == kb["jid"]
    with client.app.app_context():
        row = get_session().get(Job, ka["jid"])
        assert row is not None and row.fun == "saltutil.kill_job"

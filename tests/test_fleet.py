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
    assert b"Type <code>" not in rv.data
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
        },
    )
    assert rv.status_code == 302
    assert "/jobs/42424" in rv.headers["Location"]
    with client.app.app_context():
        job = get_session().get(Job, "42424")
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

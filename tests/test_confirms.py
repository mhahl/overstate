"""Shared destructive-action confirm dialog and its form wiring."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion, SavedJob, WatchedState
from overstate_ui.salt_client import SaltClient


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = __import__("json").loads(request.content or b"{}")
        if body.get("client") == "wheel":
            return httpx.Response(
                200,
                json={
                    "return": [
                        {
                            "data": {
                                "return": {
                                    "minions": ["web-01"],
                                    "minions_pre": ["new-01"],
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        if body.get("client") == "runner":
            return httpx.Response(200, json={"return": [{"up": [], "down": []}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    from overstate_ui.auth import _ph
    from overstate_ui.models import User

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(Minion(id="web-01", grains={}, conformity={}))
        session.add(
            Job(
                jid="j1",
                fun="test.ping",
                tgt="web-*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.add(
            SavedJob(name="ping", fun="test.ping", tgt="*", tgt_type="glob", args=[])
        )
        session.add(WatchedState(sls="baseline"))
        session.add(User(username="op", role="operator", password_hash=_ph.hash("pw")))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_confirm_dialog_renders_once_on_every_page(client):
    for path in ("/keys/", "/jobs/?tab=saved", "/users/", "/states/"):
        html = client.get(path).data.decode()
        assert html.count('id="confirm-modal"') == 1, path
        assert "data-confirmed" in html, path  # one-shot resubmit flag


def test_keys_destructive_forms_carry_confirm(client):
    html = client.get("/keys/?tab=accepted").data.decode()
    assert "data-confirm=\"Delete key for 'web-01'? A running minion" in html
    pending = client.get("/keys/?tab=pending").data.decode()
    assert "data-confirm=\"Reject key for 'new-01'?\"" in pending


def test_job_and_saved_delete_forms_carry_confirm(client):
    html = client.get("/jobs/j1").data.decode()
    assert 'data-confirm="Kill job j1?' in html
    saved = client.get("/jobs/?tab=saved").data.decode()
    assert "data-confirm=\"Delete saved job 'ping'? Past runs" in saved


def test_users_and_states_forms_carry_confirm(client):
    html = client.get("/users/").data.decode()
    assert "data-confirm=\"Delete user 'op'?\"" in html
    states = client.get("/states/").data.decode()
    assert "data-confirm=\"Stop watching 'baseline'? Conformity" in states


def test_highstate_with_typed_target_launches(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "state.highstate",
            "args": "",
            "mode": "sync",
            "confirmed": "yes",
            "confirm_tgt": "*",
        },
    )
    assert rv.status_code == 302
    assert "/jobs/" in rv.headers["Location"]


def test_grain_target_without_preview_needs_checkbox(client):
    base = {
        "tgt": "os:Fedora",
        "tgt_type": "grain",
        "fun": "state.highstate",
        "args": "",
        "mode": "sync",
        "confirmed": "yes",
        "confirm_tgt": "os:Fedora",
    }
    rv = client.post("/jobs/run", data=base)
    assert rv.status_code == 200
    assert "Fire without a match preview" in rv.data.decode()
    rv = client.post("/jobs/run", data={**base, "no_preview_ok": "on"})
    assert rv.status_code == 302


def test_orchestrate_without_confirm_does_not_queue(client):
    rv = client.post("/jobs/orchestrate/run", data={"mods": "orch.demo"})
    assert rv.status_code == 200
    assert "Review" in rv.data.decode()


def test_saltenv_rejects_shell_junk(client):
    rv = client.post(
        "/jobs/orchestrate/run",
        data={"mods": "orch.demo", "saltenv": "base;cat /etc/passwd"},
        follow_redirects=True,
    )
    assert "Saltenv" in rv.data.decode()


def test_direct_post_still_works_without_js(client):
    # The dialog only intercepts in-browser submits; plain POSTs are unchanged.
    rv = client.post("/states/unwatch/1")
    assert rv.status_code == 302
    assert "Stopped watching" in client.get(rv.headers["Location"]).data.decode()

"""Viewers see data, never operator buttons (routes already 403 them)."""

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, Minion, SavedJob, User
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
                                    "minions_pre": [],
                                    "minions_rejected": [],
                                    "minions_denied": [],
                                }
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


@pytest.fixture()
def viewer():
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
        session.add(User(username="v", role="viewer", password_hash=_ph.hash("pw")))
        session.add(Minion(id="web-01", grains={}, conformity={}))
        session.add(
            Job(
                jid="j1",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        session.add(
            SavedJob(name="ping", fun="test.ping", tgt="*", tgt_type="glob", args=[])
        )
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "v", "password": "pw"})
    c.app = app
    return c


def test_viewer_sees_no_operator_buttons(viewer):
    keys = viewer.get("/keys/?tab=accepted").data.decode()
    assert "web-01" in keys  # data stays visible
    assert ">Accept<" not in keys
    assert ">Delete<" not in keys
    minions = viewer.get("/minions/").data.decode()
    assert "Refresh inventory" not in minions
    pillar = viewer.get("/pillar/web-01").data.decode()
    assert "Capture snapshot" not in pillar
    new = viewer.get("/jobs/new").data.decode()
    assert ">Fire<" not in new
    assert "Only operators can fire jobs." in new
    saved = viewer.get("/jobs/?tab=saved").data.decode()
    assert ">Delete<" not in saved
    orch = viewer.get("/jobs/orchestrate").data.decode()
    assert "Run orchestration" not in orch
    assert viewer.get("/users/").status_code == 403


def test_operator_still_sees_buttons(viewer):
    viewer.post("/login", data={"username": "admin", "password": "pw"})
    assert ">Fire<" in viewer.get("/jobs/new").data.decode()
    assert "Refresh inventory" in viewer.get("/minions/").data.decode()
    assert "Capture snapshot" in viewer.get("/pillar/web-01").data.decode()

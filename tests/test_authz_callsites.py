"""Every Salt publish path resolves to a scoped permission (PR5, layer C).

Salt-api is mocked and the spy records every body: each mutating or
fleet-read surface must either 403 for the scoped caller without
publishing, or publish only inside the caller's scope. A new
``.local``/``.wheel``/``.runner`` call site with no gate fails here.
"""

import httpx
import pytest

from overstate_ui import auth as authmod
from overstate_ui import authz, create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Grant, Minion, Setting, User
from overstate_ui.salt_client import SaltClient

PW = "test-password"


class SaltSpy:
    def __init__(self):
        self.bodies: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        spy = self

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path == "/login":
                return httpx.Response(
                    200, json={"return": [{"token": "tok", "expire": 99}]}
                )
            body = json.loads(request.content or b"{}")
            spy.bodies.append(body)
            if body.get("client") == "wheel" and body.get("fun") == "key.list_all":
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
            if body.get("client") == "wheel" and body.get("fun") == "key.finger":
                return httpx.Response(200, json={"return": [{}]})
            tgt = body.get("tgt", "")
            mids = (
                [m.strip() for m in str(tgt).split(",")]
                if body.get("tgt_type") == "list"
                else [str(tgt)]
            )
            return httpx.Response(
                200, json={"return": [{m: {"ok": True} for m in mids}]}
            )

        return httpx.MockTransport(handler)

    def publishes(self, text: str) -> list[dict]:
        return [
            b
            for b in self.bodies
            if text in str(b.get("fun", "")) or text in str(b.get("tgt", ""))
        ]

    def wheels(self, fun: str) -> list[dict]:
        return [b for b in self.bodies if b.get("client") == "wheel" and b.get("fun") == fun]

    def runners(self, fun: str) -> list[dict]:
        return [
            b for b in self.bodies if b.get("client") == "runner" and b.get("fun") == fun
        ]


@pytest.fixture()
def env(monkeypatch):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    spy = SaltSpy()
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=spy.transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add_all(
            [
                Setting(key="rbac_mode", value="scoped"),
                Setting(key="rbac_role_fallback", value="off"),
            ]
        )
        session.add_all(
            [
                Minion(id="web-01", grains={"os": "Ubuntu"}, conformity={}),
                Minion(id="db-01", grains={"os": "Debian"}, conformity={}),
            ]
        )
        teamop = User(
            username="teamop", password_hash=authmod._ph.hash(PW), role="viewer"
        )
        session.add(teamop)
        session.flush()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=teamop.id,
                subject_group_id=None,
                role="team-operator",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.commit()
    monkeypatch.setattr(authz, "ENFORCEMENT_COMPLETE", True)
    client = app.test_client()
    client.app = app
    client.spy = spy
    return client


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def test_console_runner_needs_runner_perm(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/console/run", json={"line": "salt-run manage.status"})
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before
    assert env.spy.runners("manage.status") == []


def test_console_key_list_needs_fleet(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/console/run", json={"line": "salt-key -L"})
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before


def test_console_key_accept_is_fleet_only(env):
    login_as(env, "teamop")
    rv = env.post("/console/run", json={"line": "salt-key -a web-01"})
    assert rv.status_code == 403
    assert env.spy.wheels("key.accept") == []


def test_console_salt_constrained(env):
    login_as(env, "teamop")
    rv = env.post("/console/run", json={"line": "salt '*' test.ping"})
    assert rv.status_code == 200
    for body in env.spy.publishes("test.ping"):
        assert body.get("tgt") != "*"
        assert "db-01" not in str(body.get("tgt", "")).split(",")


def test_keys_accept_is_fleet_only(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/keys/accept", data={"id": "web-01", "tab": "pending"})
    assert rv.status_code == 403
    assert env.spy.wheels("key.accept") == []
    assert len(env.spy.bodies) == before


def test_keys_delete_scoped(env):
    login_as(env, "teamop")
    rv = env.post("/keys/delete", data={"id": "web-01", "tab": "accepted"})
    assert rv.status_code == 403
    assert env.spy.wheels("key.delete") == []


def test_schedules_add_is_define_time(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post(
        "/schedules/web-01/add",
        data={"name": "n", "function": "test.ping", "unit": "seconds", "value": "60"},
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before


def test_reactor_add_needs_reactor_write(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/reactor/add", data={"event": "e", "sls": "s"})
    assert rv.status_code == 403
    assert env.spy.runners("reactor.add") == []
    assert len(env.spy.bodies) == before


def test_reactor_delete_needs_reactor_write(env):
    login_as(env, "teamop")
    rv = env.post("/reactor/delete", data={"event": "e"})
    assert rv.status_code == 403
    assert env.spy.runners("reactor.delete") == []


def test_files_sync_needs_file_sync(env):
    login_as(env, "teamop")
    rv = env.post("/files/sync-modules")
    assert rv.status_code == 403
    assert env.spy.runners("saltutil.sync_all") == []


def test_files_repo_needs_file_git(env):
    login_as(env, "teamop")
    rv = env.post("/files/repo/clone", data={"remote": "https://example.invalid/r.git"})
    assert rv.status_code == 403


def test_refresh_publishes_scoped_list(env):
    login_as(env, "teamop")
    rv = env.post("/minions/refresh")
    assert rv.status_code in (200, 302)
    grains = env.spy.publishes("grains.item")
    assert grains, "expected a scoped grains refresh"
    for body in grains:
        assert body.get("tgt") != "*"
        assert "db-01" not in str(body.get("tgt", "")).split(",")


def test_onboard_needs_onboard_perm(env):
    login_as(env, "teamop")
    rv = env.get("/minions/onboard")
    assert rv.status_code == 403


def test_remove_needs_remove_perm(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/minions/db-01/remove")
    assert rv.status_code == 403
    assert env.spy.wheels("key.delete") == []
    assert len(env.spy.bodies) == before


def test_minion_detail_out_of_scope_is_403(env):
    login_as(env, "teamop")
    rv = env.get("/minions/db-01")
    assert rv.status_code == 403


def test_group_create_needs_group_write(env):
    login_as(env, "teamop")
    rv = env.post("/groups/", data={"name": "x"})
    assert rv.status_code == 403


def test_admin_surfaces_need_fleet_perms(env):
    login_as(env, "teamop")
    assert env.get("/users/").status_code == 403
    assert env.get("/settings/").status_code == 403
    assert env.get("/settings/master/").status_code == 403
    assert env.get("/reactor/").status_code == 403
    assert env.get("/files/").status_code == 403


def test_worker_refresh_denies_unknown_user(env, monkeypatch):
    from contextlib import nullcontext

    from overstate_ui import tasks_salt

    monkeypatch.setattr(tasks_salt, "isolated_app", lambda: nullcontext())
    with env.app.app_context():
        result = tasks_salt.refresh_inventory_task(None, "ghost")
    assert result.get("error"), f"expected denial, got {result!r}"


def test_worker_fleet_refresh_denies_scoped_user(env, monkeypatch):
    from contextlib import nullcontext

    from overstate_ui import tasks_salt

    monkeypatch.setattr(tasks_salt, "isolated_app", lambda: nullcontext())
    with env.app.app_context():
        result = tasks_salt.refresh_inventory_task(None, "teamop")
    assert result.get("error"), f"expected denial, got {result!r}"

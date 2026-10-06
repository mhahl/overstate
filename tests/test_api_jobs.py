"""Service accounts, Bearer tokens, POST /api/jobs/run (PR8).

Scoped mode only: a token fires through launch() as its service-account
owner, so constrain_target and audit attribution apply unchanged. An
unpinned token body is limited to the read class; CONFIRM_FUNS ride a
pin to a saved job. Salt-api is mocked; the spy records every body.
"""

import datetime as dt
import secrets

import httpx
import pytest

from overstate_ui import api as apimod
from overstate_ui import auth as authmod
from overstate_ui import authz, create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import (
    ApiToken,
    AuditEvent,
    Grant,
    Job,
    Minion,
    SavedJob,
    Setting,
    User,
)
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
            return httpx.Response(200, json={"return": [{"jid": "1"}]})

        return httpx.MockTransport(handler)

    def publishes(self, fun: str) -> list[dict]:
        return [b for b in self.bodies if b.get("fun") == fun]


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
        svc = User(
            username="ci", password_hash=None, role="none", kind="service"
        )
        session.add(svc)
        session.flush()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=svc.id,
                subject_group_id=None,
                role="team-state",
                scope_kind="glob",
                scope_value="web-*",
                source="manual",
            )
        )
        session.add(
            SavedJob(
                name="deploy",
                fun="state.apply",
                tgt="*",
                tgt_type="glob",
                args=[],
                batch=None,
            )
        )
        session.commit()
        svc_id = svc.id
    monkeypatch.setattr(authz, "ENFORCEMENT_COMPLETE", True)
    apimod._attempts.clear()
    client = app.test_client()
    client.app = app
    client.spy = spy
    client.svc_id = svc_id
    with app.app_context():
        client.saved_id = get_session().query(SavedJob).one().id
    return client


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def _mint(client, name="t1", pin=None, expired=False, revoked=False):
    """Insert a token row directly; return (raw, row id)."""
    prefix = secrets.token_hex(6)
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    with client.app.app_context():
        session = get_session()
        row = ApiToken(
            user_id=client.svc_id,
            name=name,
            token_prefix=prefix,
            token_hash=authmod._ph.hash(raw),
            saved_job_id=pin,
            expires_at=(
                dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=1)
                if expired
                else None
            ),
            revoked_at=(
                dt.datetime.now(dt.UTC).replace(tzinfo=None) if revoked else None
            ),
        )
        session.add(row)
        session.commit()
        return raw, row.id


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_token_pinned_saved_job(env):
    raw, _ = _mint(env, pin=env.saved_id)
    rv = env.post("/api/jobs/run", json={"saved_id": env.saved_id}, headers=_auth(raw))
    assert rv.status_code == 200, rv.get_json()
    jid = rv.get_json()["jid"]
    # The saved target "*" was constrained to the token's scope, never
    # published raw; the Job row is attributed to the service account.
    fires = env.spy.publishes("state.apply")
    assert len(fires) == 1
    assert fires[0]["tgt"] == "web-01"
    with env.app.app_context():
        job = get_session().get(Job, jid)
        assert job is not None and job.user == "ci"
        assert job.tgt_requested == "*"


def test_unpinned_allows_read_class(env):
    raw, _ = _mint(env)
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "test.ping", "tgt": "*", "tgt_type": "glob"},
        headers=_auth(raw),
    )
    assert rv.status_code == 200, rv.get_json()
    fires = env.spy.publishes("test.ping")
    assert len(fires) == 1
    assert fires[0]["tgt"] == "web-01"


def test_unpinned_rejects_state_apply(env):
    raw, _ = _mint(env)
    before = len(env.spy.bodies)
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "state.apply", "tgt": "web-01", "tgt_type": "list"},
        headers=_auth(raw),
    )
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "function-not-allowed"
    assert len(env.spy.bodies) == before


def test_unpinned_rejects_schedule_add(env):
    raw, _ = _mint(env)
    before = len(env.spy.bodies)
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "schedule.add", "tgt": "web-01", "tgt_type": "list"},
        headers=_auth(raw),
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before


def test_pin_mismatch_is_404(env):
    raw, _ = _mint(env, pin=env.saved_id)
    rv = env.post(
        "/api/jobs/run", json={"saved_id": env.saved_id + 999}, headers=_auth(raw)
    )
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "unknown-saved-job"
    with env.app.app_context():
        row = (
            get_session()
            .query(AuditEvent)
            .filter_by(action="deny", detail="pin-mismatch")
            .one()
        )
        assert row.user == "ci"


def test_missing_bearer_is_401(env):
    login_as(env, "admin")
    rv = env.post("/api/jobs/run", json={"fun": "test.ping"})
    assert rv.status_code == 401
    assert rv.get_json()["error"] == "bearer-required"


def test_bad_secret_is_401(env):
    raw, _ = _mint(env)
    prefix = raw.split("_", 1)[0]
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "test.ping"},
        headers={"Authorization": f"Bearer {prefix}_wrong"},
    )
    assert rv.status_code == 401


def test_revoked_token_is_401(env):
    raw, _ = _mint(env, revoked=True)
    rv = env.post("/api/jobs/run", json={"fun": "test.ping"}, headers=_auth(raw))
    assert rv.status_code == 401


def test_expired_token_is_401(env):
    raw, _ = _mint(env, expired=True)
    rv = env.post("/api/jobs/run", json={"fun": "test.ping"}, headers=_auth(raw))
    assert rv.status_code == 401


def test_legacy_mode_rejects_api(env):
    raw, _ = _mint(env)
    with env.app.app_context():
        session = get_session()
        session.query(Setting).filter_by(key="rbac_mode").one().value = "legacy"
        session.commit()
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "test.ping", "tgt": "*", "tgt_type": "glob"},
        headers=_auth(raw),
    )
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "api-requires-scoped-mode"


def test_out_of_scope_fire_is_403_with_actor_deny(env):
    raw, _ = _mint(env)
    before = len(env.spy.bodies)
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "test.ping", "tgt": "db-01", "tgt_type": "list"},
        headers=_auth(raw),
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before
    with env.app.app_context():
        rows = (
            get_session()
            .query(AuditEvent)
            .filter_by(action="deny", user="ci")
            .all()
        )
        # launch wrote exactly one deny row naming the service account.
        assert len(rows) == 1


def test_rate_limit_429(env, monkeypatch):
    monkeypatch.setattr(apimod, "TOKEN_RATE_LIMIT", 3)
    raw, _ = _mint(env)
    body = {"fun": "test.ping", "tgt": "web-01", "tgt_type": "list"}
    for _ in range(3):
        assert env.post("/api/jobs/run", json=body, headers=_auth(raw)).status_code == 200
    rv = env.post("/api/jobs/run", json=body, headers=_auth(raw))
    assert rv.status_code == 429
    assert rv.get_json()["error"] == "rate-limited"


def test_service_account_ui_create_and_revoke(env):
    login_as(env, "admin")
    rv = env.post("/users/service-accounts", data={"name": "nightly"})
    assert rv.status_code == 302
    with env.app.app_context():
        user = get_session().query(User).filter_by(username="nightly").one()
        assert user.kind == "service"
        assert user.role == "none"
        uid = user.id
    rv = env.post(
        f"/users/service-accounts/{uid}/tokens",
        data={"name": "ci-token", "saved_job_id": str(env.saved_id)},
    )
    assert rv.status_code == 302
    with env.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    shown = [msg for _, msg in flashes if "ci-token" in msg]
    assert len(shown) == 1 and env.saved_id is not None
    raw = shown[0].rsplit(": ", 1)[1]
    with env.app.app_context():
        row = get_session().query(ApiToken).filter_by(user_id=uid).one()
        tid = row.id
        assert row.saved_job_id == env.saved_id
        assert raw not in (row.token_hash or "")
        assert authmod._ph.verify(row.token_hash, raw)
        assert row.token_prefix == raw.split("_", 1)[0]
    # Show-once: the first render displays the secret, the next one
    # carries no trace of it.
    page = env.get("/users/")
    assert page.status_code == 200
    assert raw.encode() in page.data
    again = env.get("/users/")
    assert again.status_code == 200
    assert raw.encode() not in again.data
    rv = env.post(f"/users/service-accounts/{uid}/tokens/{tid}/revoke")
    assert rv.status_code == 302
    rv = env.post("/api/jobs/run", json={"fun": "test.ping"}, headers=_auth(raw))
    assert rv.status_code == 401


def test_service_account_cannot_login(env):
    with env.app.app_context():
        session = get_session()
        session.add(User(username="bot", password_hash=None, role="none", kind="service"))
        session.commit()
    rv = env.post("/login", data={"username": "bot", "password": "anything"})
    assert rv.status_code in (200, 400)
    assert env.get("/users/").status_code == 302


def test_pinned_token_rejects_unpinned_body(env):
    raw, _ = _mint(env, pin=env.saved_id)
    before = len(env.spy.bodies)
    rv = env.post(
        "/api/jobs/run",
        json={"fun": "test.ping", "tgt": "*", "tgt_type": "glob"},
        headers=_auth(raw),
    )
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "unknown-saved-job"
    assert len(env.spy.bodies) == before

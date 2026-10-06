"""Scoped target constraint at the publish paths (PR5, layer A).

Scoped mode with the salt-api mocked: ``*`` must never reach salt-api
for a scoped caller, kills and mine reads intersect locally, and ssh
stays fleet-only.
"""

import httpx
import pytest

from overstate_ui import auth as authmod
from overstate_ui import authz, create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, Grant, Job, Minion, Setting, User
from overstate_ui.salt_client import SaltClient

PW = "test-password"


class SaltSpy:
    """MockTransport recording every salt-api body it sees."""

    def __init__(self):
        self.bodies: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        spy = self

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(
                    200, json={"return": [{"token": "tok", "expire": 99}]}
                )
            body = __import__("json").loads(request.content or b"{}")
            spy.bodies.append(body)
            client = body.get("client")
            if client == "local_async":
                return httpx.Response(
                    200, json={"return": [{"jid": "20240101120000000001", "minions": []}]}
                )
            if client == "ssh":
                return httpx.Response(
                    200, json={"return": [{"web-01": True, "db-01": True}]}
                )
            fun = body.get("fun", "")
            tgt = body.get("tgt", "")
            mids = [m.strip() for m in str(tgt).split(",")] if body.get("tgt_type") == "list" else [str(tgt)]
            if fun == "mine.get":
                reader = tgt if isinstance(tgt, str) else "web-01"
                return httpx.Response(
                    200, json={"return": [{reader: {"web-01": {"ntp": ["x"]}}}]}
                )
            if fun == "saltutil.kill_job":
                return httpx.Response(200, json={"return": [{"jid": "999"}]})
            return httpx.Response(
                200, json={"return": [{m: {"ok": True} for m in mids}]}
            )

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
        seer = User(username="seer", password_hash=authmod._ph.hash(PW), role="viewer")
        session.add(seer)
        session.flush()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=seer.id,
                subject_group_id=None,
                role="secrets-reader",
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


def test_scoped_allow_list_target(env):
    login_as(env, "teamop")
    rv = env.post(
        "/jobs/run",
        data={"tgt": "web-01", "tgt_type": "list", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 302
    published = env.spy.publishes("test.ping")
    assert published, "no salt publish recorded"
    assert published[0]["tgt"] == "web-01"
    assert published[0]["tgt_type"] == "list"
    with env.app.app_context():
        job = get_session().query(Job).filter_by(fun="test.ping").one()
        assert job.tgt == "web-01"
        assert (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "run:test.ping")
            .count()
            == 1
        )


def test_scoped_star_does_not_escape(env):
    login_as(env, "teamop")
    rv = env.post(
        "/jobs/run",
        data={"tgt": "*", "tgt_type": "glob", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 302
    published = env.spy.publishes("test.ping")
    assert published
    for body in published:
        assert body["tgt"] != "*"
        assert body["tgt_type"] == "list"
        assert "db-01" not in body["tgt"].split(",")
    with env.app.app_context():
        row = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "job-constrained")
            .one()
        )
        assert "db-01" in (row.detail or "")
        job = get_session().get(Job, row.jid)
        assert job.tgt == "web-01"
        assert job.tgt_requested == "*"


def test_scoped_star_empty_is_403(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post(
        "/jobs/run",
        data={"tgt": "db-*", "tgt_type": "glob", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before
    with env.app.app_context():
        assert get_session().query(Job).filter_by(fun="test.ping").count() == 0
        deny = (
            get_session()
            .query(AuditEvent)
            .filter_by(outcome="deny")
            .order_by(AuditEvent.id.desc())
            .first()
        )
        assert deny is not None and deny.detail == "empty-intersection"


def test_scoped_compound_403(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "G@os:Ubuntu",
            "tgt_type": "compound",
            "fun": "test.ping",
            "mode": "async",
        },
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before


def test_fleet_star_unchanged(env):
    login_as(env, "admin")
    rv = env.post(
        "/jobs/run",
        data={"tgt": "*", "tgt_type": "glob", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 302
    published = env.spy.publishes("test.ping")
    assert published and published[0]["tgt"] == "*"
    assert published[0]["tgt_type"] == "glob"


def test_kill_unevaluable_scoped_403(env):
    with env.app.app_context():
        get_session().add(
            Job(
                jid="20240101120000000011",
                fun="test.ping",
                tgt="G@os:Ubuntu",
                tgt_type="compound",
                user="admin",
                complete=False,
            )
        )
        get_session().commit()
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/jobs/20240101120000000011/kill")
    assert rv.status_code == 403
    assert not env.spy.publishes("saltutil.kill_job")
    assert len(env.spy.bodies) == before


def test_kill_fleet_republishes(env):
    with env.app.app_context():
        get_session().add(
            Job(
                jid="20240101120000000012",
                fun="test.ping",
                tgt="web-01",
                tgt_type="list",
                user="admin",
                complete=False,
            )
        )
        get_session().commit()
    login_as(env, "admin")
    rv = env.post("/jobs/20240101120000000012/kill")
    assert rv.status_code == 302
    published = env.spy.publishes("saltutil.kill_job")
    assert published and published[0]["tgt"] == "web-01"


def test_ssh_fleet_only(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "web-01",
            "tgt_type": "list",
            "fun": "test.ping",
            "mode": "sync",
            "via": "ssh",
        },
    )
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before
    login_as(env, "admin")
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "web-01",
            "tgt_type": "list",
            "fun": "test.ping",
            "mode": "sync",
            "via": "ssh",
        },
    )
    assert rv.status_code == 302
    assert any(b.get("client") == "ssh" for b in env.spy.bodies)


def test_mine_constrained(env):
    login_as(env, "seer")
    rv = env.get("/mine/?tgt=*&tgt_type=glob&fun=network")
    assert rv.status_code == 200
    published = [b for b in env.spy.bodies if b.get("fun") == "mine.get"]
    assert published, "mine.get never published"
    assert published[0]["tgt"] == "web-01"
    html = rv.data.decode()
    assert "db-01" not in html
    rv = env.get("/mine/?tgt=G@os:Ubuntu&tgt_type=compound&fun=network")
    assert rv.status_code == 403


def test_beacon_kwargs_forced_on_wire(env):
    login_as(env, "teamop")
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "web-01",
            "tgt_type": "list",
            "fun": "beacons.list",
            "args": "include_pillar=True",
            "mode": "async",
        },
    )
    assert rv.status_code == 302
    published = env.spy.publishes("beacons.list")
    assert published
    body = published[0]
    assert body["kwarg"]["include_pillar"] is False
    assert body["kwarg"]["include_opts"] is False
    assert not any(
        str(a).split("=")[0] in ("include_pillar", "include_opts")
        for a in body.get("arg", [])
    )


def test_console_beacon_strips_positional_token(env):
    login_as(env, "teamop")
    rv = env.post(
        "/console/run", json={"line": "salt web-01 beacons.list include_pillar=True"}
    )
    assert rv.status_code == 200, rv.data.decode()[:300]
    published = env.spy.publishes("beacons.list")
    assert published
    assert published[0]["kwarg"]["include_pillar"] is False
    assert not any(
        str(a).split("=")[0] in ("include_pillar", "include_opts")
        for a in published[0].get("arg", [])
    )


def test_console_sync_redacts_without_body_perm(env, monkeypatch):
    import overstate_ui.console as consolemod
    from overstate_ui.models import JobReturn

    with env.app.app_context():
        get_session().add(
            Job(
                jid="redact-jid-1",
                fun="beacons.list",
                tgt="web-01",
                tgt_type="list",
                user="teamop",
            )
        )
        get_session().add(
            JobReturn(
                jid="redact-jid-1",
                minion_id="web-01",
                success=True,
                retcode=0,
                payload="beacon-config-with-pillar-secret",
            )
        )
        get_session().commit()
    monkeypatch.setattr(consolemod, "launch", lambda *a, **k: "redact-jid-1")
    login_as(env, "teamop")
    rv = env.post("/console/run", json={"line": "salt web-01 beacons.list --sync"})
    assert rv.status_code == 200
    out = rv.get_json()["output"]
    assert "web-01: ok" in out
    assert "beacon-config-with-pillar-secret" not in out


def test_console_key_list_fleet_only(env):
    login_as(env, "teamop")
    rv = env.post("/console/run", json={"line": "salt-key -L"})
    assert rv.status_code == 403


def test_batch_recheck_drops_minion(env, monkeypatch):
    import overstate_ui.tasks_batch as batchmod
    from overstate_ui.models import User as UserModel

    seen: list = []

    class FakeClient:
        def local(self, tgt, fun, **kwargs):
            seen.append((list(tgt), fun, kwargs.get("kwarg")))
            return [{"jid": "wave-jid-1"}]

    monkeypatch.setattr("overstate_ui.tasks.build_client", lambda: FakeClient())
    with env.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="batch-g1",
                fun="test.ping",
                tgt="web-01,db-01",
                tgt_type="list",
                user="teamop",
                batch_group="g1",
                batch_state={},
            )
        )
        session.commit()
        result = batchmod.run_wave_batch(
            "g1", [["web-01"], ["db-01"]], "test.ping", [], 10, "teamop", wave_timeout=0
        )
        assert result["status"] == "complete"
    assert [t for t, _, _ in seen] == [["web-01"]]
    # Revoke the grant: the whole permission disappears, batch stops.
    with env.app.app_context():
        teamop = session.query(UserModel).filter_by(username="teamop").one()
        session.query(Grant).filter_by(subject_user_id=teamop.id).delete()
        session.commit()
        seen.clear()
        result = batchmod.run_wave_batch(
            "g1", [["web-01"]], "test.ping", [], 1, "teamop", wave_timeout=0
        )
        assert result["status"] == "stopped"
        assert seen == []
        assert (
            session.query(AuditEvent)
            .filter_by(action="batch-stopped-authz")
            .count()
            == 1
        )


def _fleet_user(client, username: str, role: str = "operator"):
    with client.app.app_context():
        session = get_session()
        user = User(
            username=username, password_hash=authmod._ph.hash(PW), role="viewer"
        )
        session.add(user)
        session.flush()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=user.id,
                subject_group_id=None,
                role=role,
                scope_kind="fleet",
                scope_value="*",
                source="manual",
            )
        )
        session.commit()


def test_fleet_confirm_renders(env):
    _fleet_user(env, "fleetop")
    login_as(env, "fleetop")
    rv = env.post(
        "/jobs/run",
        data={"tgt": "*", "tgt_type": "glob", "fun": "state.apply", "mode": "async"},
    )
    assert rv.status_code == 200


def test_fleet_mine_page(env):
    _fleet_user(env, "fleetview", role="viewer")
    login_as(env, "fleetview")
    rv = env.get("/mine/?tgt=*&tgt_type=glob&fun=network.ip_addrs")
    assert rv.status_code == 200
    published = env.spy.publishes("mine.get")
    # The mine query publishes to the snapshot-head reader with the
    # fleet target unchanged inside the args.
    assert published and published[0]["arg"][0] == "*"
    assert published[0]["tgt"] in ("web-01", "db-01")


def test_fleet_batch_starts(env):
    _fleet_user(env, "fleetop")
    login_as(env, "fleetop")
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "async",
            "batch_mode": "count",
            "batch_size": "1",
            "stop_after": "1",
        },
    )
    assert rv.status_code == 302
    assert "batch-" in rv.headers["Location"]
    with env.app.app_context():
        assert get_session().query(Job).filter(Job.jid.like("batch-%")).count() >= 1


def test_fleet_schedule_add_from_job_form(env):
    _fleet_user(env, "fleetop")
    login_as(env, "fleetop")
    rv = env.post(
        "/jobs/run",
        data={
            "tgt": "web-01",
            "tgt_type": "list",
            "fun": "schedule.add",
            "mode": "async",
            "args": "function=test.ping name=ci interval=60",
        },
    )
    assert rv.status_code == 302
    assert env.spy.publishes("schedule.add")


def test_kill_snapshot_covering_glob_is_403(env):
    # A glob-* grant is not a fleet grant: it must not kill a stored
    # fleet * job, even when it covers every snapshot id today.
    with env.app.app_context():
        session = get_session()
        starop = User(
            username="starop", password_hash=authmod._ph.hash(PW), role="viewer"
        )
        session.add(starop)
        session.flush()
        session.add(
            Grant(
                subject_kind="user",
                subject_user_id=starop.id,
                subject_group_id=None,
                role="team-operator",
                scope_kind="glob",
                scope_value="*",
                source="manual",
            )
        )
        session.add(
            Job(
                jid="20240101120000000013",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.commit()
    login_as(env, "starop")
    before = len(env.spy.bodies)
    rv = env.post("/jobs/20240101120000000013/kill")
    assert rv.status_code == 403
    assert not env.spy.publishes("saltutil.kill_job")
    assert len(env.spy.bodies) == before

"""Scoped read filtering (PR5, layer B).

Scoped mode with salt-api mocked: detail pages 403 on out-of-scope ids,
returns and targets render only inside the scope, state bodies stay
hidden without job.run.state, and zero-grant users get an empty
dashboard shell with no probes.
"""

import httpx
import pytest

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
    JobReturn,
    Minion,
    SavedJob,
    Setting,
    User,
)
from overstate_ui.salt_client import SaltClient

PW = "test-password"
SECRET = "SECRET_MARKER_abc123"


class SaltSpy:
    """MockTransport recording every salt-api body it sees."""

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
            client = body.get("client")
            if client == "local_async":
                return httpx.Response(
                    200, json={"return": [{"jid": "20240101120000000001", "minions": []}]}
                )
            fun = body.get("fun", "")
            if client == "runner" and fun == "jobs.lookup_jid":
                return httpx.Response(200, json={"return": [{}]})
            if client == "runner" and fun == "manage.status":
                return httpx.Response(
                    200, json={"return": [{"up": ["web-01"], "down": ["db-01"]}]}
                )
            if client == "wheel":
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                "data": {
                                    "return": {
                                        "minions": ["web-01", "db-01"],
                                        "minions_pre": [],
                                        "minions_rejected": [],
                                        "minions_denied": [],
                                    }
                                }
                            }
                        ]
                    },
                )
            tgt = body.get("tgt", "")
            mids = (
                [m.strip() for m in str(tgt).split(",")]
                if body.get("tgt_type") == "list"
                else [str(tgt)]
            )
            if fun == "pillar.items":
                return httpx.Response(
                    200,
                    json={"return": [{m: {"secret": SECRET} for m in mids}]},
                )
            if fun == "beacons.list":
                # Salt's default shape is a YAML string carrying
                # pillar-sourced config; the pillar-excluded call is a
                # real mapping.
                if (body.get("kwarg") or {}).get("include_pillar") is False:
                    return httpx.Response(
                        200, json={"return": [{m: {"local": []} for m in mids}]}
                    )
                return httpx.Response(
                    200,
                    json={
                        "return": [
                            {
                                m: "ps:\n  processes:\n    BEACON_SECRET_abc123"
                                for m in mids
                            }
                        ]
                    },
                )
            return httpx.Response(
                200, json={"return": [{m: {"ok": True} for m in mids}]}
            )

        return httpx.MockTransport(handler)

    def publishes(self, fun: str) -> list[dict]:
        return [b for b in self.bodies if b.get("fun") == fun]


def _grant(session, user, role, scope_value="web-*"):
    session.add(
        Grant(
            subject_kind="user",
            subject_user_id=user.id,
            subject_group_id=None,
            role=role,
            scope_kind="glob",
            scope_value=scope_value,
            source="manual",
        )
    )


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
                Minion(
                    id="web-01",
                    grains={"os": "Ubuntu", "saltversion": "3008"},
                    conformity={},
                ),
                Minion(
                    id="db-01",
                    grains={"os": "Debian", "saltversion": "3006"},
                    conformity={},
                ),
            ]
        )
        teamop = User(
            username="teamop", password_hash=authmod._ph.hash(PW), role="viewer"
        )
        session.add(teamop)
        session.flush()
        _grant(session, teamop, "team-operator")
        seer = User(username="seer", password_hash=authmod._ph.hash(PW), role="viewer")
        session.add(seer)
        session.flush()
        _grant(session, seer, "secrets-reader")
        lead = User(username="lead", password_hash=authmod._ph.hash(PW), role="viewer")
        session.add(lead)
        session.flush()
        _grant(session, lead, "team-lead")
        zero = User(username="zero", password_hash=authmod._ph.hash(PW), role="none")
        session.add(zero)
        session.commit()
    monkeypatch.setattr(authz, "ENFORCEMENT_COMPLETE", True)
    client = app.test_client()
    client.app = app
    client.spy = spy
    return client


def login_as(client, username: str):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": PW})


def _seed_state_job():
    session = get_session()
    job = Job(
        jid="202401010000000001",
        fun="state.apply",
        tgt="web-01,db-01",
        tgt_type="list",
        user="admin",
        complete=True,
    )
    session.add(job)
    session.add_all(
        [
            JobReturn(
                jid=job.jid,
                minion_id="web-01",
                success=True,
                retcode=0,
                payload={"db_password": SECRET},
            ),
            JobReturn(
                jid=job.jid,
                minion_id="db-01",
                success=False,
                retcode=1,
                payload={"db_password": SECRET},
            ),
        ]
    )
    session.commit()
    return job.jid


def test_pillar_detail_out_of_scope_is_403(env):
    login_as(env, "teamop")
    rv = env.get("/pillar/db-01")
    assert rv.status_code == 403


def test_pillar_detail_in_scope_stays_inside(env):
    login_as(env, "seer")
    rv = env.get("/pillar/web-01")
    assert rv.status_code == 200
    for body in env.spy.publishes("pillar.items"):
        assert body.get("tgt") != "*"
        assert "db-01" not in str(body.get("tgt", "")).split(",")


def test_pillar_capture_needs_capture_perm(env):
    login_as(env, "teamop")
    before = len(env.spy.bodies)
    rv = env.post("/pillar/web-01/capture")
    assert rv.status_code == 403
    assert len(env.spy.bodies) == before


def test_mine_needs_mine_read(env):
    login_as(env, "teamop")
    rv = env.get("/mine/?tgt=web-*&tgt_type=glob&fun=network.ips")
    assert rv.status_code == 403
    assert not env.spy.publishes("mine.get")


def test_mine_constrained_publish(env):
    login_as(env, "seer")
    rv = env.get("/mine/?tgt=*&tgt_type=glob&fun=network.ips")
    assert rv.status_code == 200
    for body in env.spy.publishes("mine.get"):
        assert body.get("tgt") != "*"
        assert "db-01" not in str(body.get("tgt", "")).split(",")


def test_job_returns_filtered_to_scope(env):
    with env.app.app_context():
        jid = _seed_state_job()
    login_as(env, "teamop")
    rv = env.get(f"/jobs/{jid}")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert "web-01" in html
    assert "db-01" not in html


def test_job_out_of_scope_is_403(env):
    with env.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202401010000000002",
                fun="test.ping",
                tgt="db-01",
                tgt_type="list",
                user="admin",
                complete=True,
            )
        )
        session.commit()
    login_as(env, "teamop")
    rv = env.get("/jobs/202401010000000002")
    assert rv.status_code == 403


def test_state_body_hidden_without_run_state(env):
    with env.app.app_context():
        jid = _seed_state_job()
    login_as(env, "teamop")
    rv = env.get(f"/jobs/{jid}")
    assert rv.status_code == 200
    assert SECRET not in rv.get_data(as_text=True)


def test_state_body_visible_with_run_state(env):
    with env.app.app_context():
        jid = _seed_state_job()
    login_as(env, "lead")
    rv = env.get(f"/jobs/{jid}")
    assert rv.status_code == 200
    assert SECRET in rv.get_data(as_text=True)


def test_dashboard_zero_grant_empty_shell(env):
    login_as(env, "zero")
    before = len(env.spy.bodies)
    rv = env.get("/")
    assert rv.status_code == 200
    # Empty shell: no probes enqueued, no counts rendered.
    assert len(env.spy.bodies) == before
    html = rv.get_data(as_text=True)
    assert "db-01" not in html
    assert "web-01" not in html
    rv = env.get("/dashboard/panels")
    assert rv.status_code == 403


def test_dashboard_counts_scoped(env):
    login_as(env, "teamop")
    rv = env.get("/")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert "3008" in html
    assert "3006" not in html


def _seed_audit_rows():
    session = get_session()
    session.add_all(
        [
            AuditEvent(user="lead", action="run:test.ping", outcome="allow"),
            AuditEvent(
                user="admin", action="run:test.ping", outcome="allow", minion_id=None
            ),
            AuditEvent(
                user="admin",
                action="pillar-capture",
                outcome="allow",
                minion_id="web-01",
            ),
            AuditEvent(
                user="admin",
                action="pillar-capture",
                outcome="allow",
                minion_id="db-01",
            ),
            AuditEvent(
                user="lead",
                action="job-constrained",
                outcome="allow",
                permission="job.run.read",
                detail="db-01,db-02",
            ),
        ]
    )
    session.commit()


def test_audit_scoped_filter_and_redaction(env):
    with env.app.app_context():
        _seed_audit_rows()
    login_as(env, "lead")
    rv = env.get("/audit/")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    # Own rows and in-scope minion rows stay visible...
    assert "pillar-capture" in html
    # ...the fleet history row and the out-of-scope minion row do not.
    assert html.count("pillar-capture") == 1
    # job-constrained detail names dropped ids: the actor's own row is
    # visible but the dropped names are not (redacted at read).
    assert "job-constrained" in html
    assert "db-02" not in html
    with env.app.app_context():
        from overstate_ui.audit import redact_constrained_detail

        assert redact_constrained_detail("db-01,db-02") == (
            "2 minion(s) outside the actor's scope"
        )
        assert redact_constrained_detail(None) is None
        row = get_session().query(AuditEvent).filter_by(action="job-constrained").one()
        assert row.detail == "db-01,db-02"


def test_audit_needs_audit_read(env):
    login_as(env, "teamop")
    rv = env.get("/audit/")
    assert rv.status_code == 403


def test_events_scoped_gate(env):
    login_as(env, "teamop")
    rv = env.get("/events/")
    assert rv.status_code == 403
    rv = env.get("/events/stream?tag=salt/job")
    assert rv.status_code == 403


def test_states_watch_needs_watch_perm(env):
    login_as(env, "teamop")
    rv = env.post("/states/watch", data={"sls": "ntp"})
    assert rv.status_code == 403
    with env.app.app_context():
        from overstate_ui.models import WatchedState

        assert get_session().query(WatchedState).count() == 0


def test_states_index_filtered(env):
    login_as(env, "teamop")
    rv = env.get("/states/")
    assert rv.status_code == 200
    assert "db-01" not in rv.get_data(as_text=True)


def _seed_saved(name, tgt, tgt_type="glob"):
    session = get_session()
    saved = SavedJob(name=name, fun="test.ping", tgt=tgt, tgt_type=tgt_type)
    session.add(saved)
    session.commit()
    return saved.id


def test_delete_saved_in_scope(env):
    with env.app.app_context():
        sid = _seed_saved("webping", "web-*")
    login_as(env, "teamop")
    rv = env.post(f"/jobs/saved/{sid}/delete")
    assert rv.status_code == 302
    with env.app.app_context():
        assert get_session().get(SavedJob, sid) is None


def test_delete_saved_out_of_scope_is_403(env):
    with env.app.app_context():
        sid = _seed_saved("dbping", "db-*")
    login_as(env, "teamop")
    rv = env.post(f"/jobs/saved/{sid}/delete")
    assert rv.status_code == 403
    with env.app.app_context():
        assert get_session().get(SavedJob, sid) is not None


def test_delete_saved_pinned_is_409(env):
    with env.app.app_context():
        sid = _seed_saved("pinned", "web-*")
        session = get_session()
        admin = session.query(User).filter_by(username="admin").one()
        session.add(
            ApiToken(
                user_id=admin.id,
                name="ci",
                token_hash="argon2:deadbeef",
                token_prefix="ci123",
                saved_job_id=sid,
            )
        )
        session.commit()
    login_as(env, "teamop")
    rv = env.post(f"/jobs/saved/{sid}/delete")
    assert rv.status_code == 409
    with env.app.app_context():
        assert get_session().get(SavedJob, sid) is not None


def test_delete_saved_partial_overlap_is_403(env):
    with env.app.app_context():
        sid = _seed_saved("wide", "*")
    login_as(env, "teamop")
    rv = env.post(f"/jobs/saved/{sid}/delete")
    assert rv.status_code == 403
    with env.app.app_context():
        assert get_session().get(SavedJob, sid) is not None


def test_saved_list_hides_partial_overlap(env):
    with env.app.app_context():
        _seed_saved("wide", "*")
        _seed_saved("webping", "web-*")
    login_as(env, "teamop")
    html = env.get("/jobs/?tab=saved").data.decode()
    assert "webping" in html
    assert "wide" not in html


def test_audit_scoped_to_audit_read_grants(env):
    with env.app.app_context():
        session = get_session()
        lead = session.query(User).filter_by(username="lead").one()
        # A second, wider grant with no audit.read must not widen the
        # audit view: only the audit.read scope counts. (The table
        # renders actions, so the rows use distinct actions.)
        _grant(session, lead, "team-operator", scope_value="db-*")
        session.add_all(
            [
                AuditEvent(
                    user="admin",
                    action="web-action",
                    outcome="allow",
                    minion_id="web-01",
                ),
                AuditEvent(
                    user="admin",
                    action="db-action",
                    outcome="allow",
                    minion_id="db-01",
                ),
            ]
        )
        session.commit()
    login_as(env, "lead")
    html = env.get("/audit/").data.decode()
    assert "web-action" in html
    assert "db-action" not in html


def test_beacons_tab_string_hides_pillar(env):
    login_as(env, "teamop")
    html = env.get("/minions/web-01?tab=beacons").data.decode()
    assert "BEACON_SECRET_abc123" not in html
    for body in env.spy.publishes("beacons.list"):
        assert (body.get("kwarg") or {}).get("include_pillar") is False


def test_raw_tab_hides_schedule_without_perm(env):
    login_as(env, "seer")
    before = len(env.spy.bodies)
    rv = env.get("/minions/web-01?tab=raw")
    assert rv.status_code == 200
    assert [b for b in env.spy.bodies[before:] if b.get("fun") == "schedule.list"] == []


def test_states_refresh_passes_user(env, monkeypatch):
    import overstate_ui.tasks as taskmod

    seen = {}

    def fake_queue(func, *args, **kwargs):
        seen["func"] = func
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(taskmod, "queue_or_none", fake_queue)
    with env.app.app_context():
        session = get_session()
        seer = session.query(User).filter_by(username="seer").one()
        _grant(session, seer, "team-operator")
        session.commit()
    login_as(env, "seer")
    rv = env.post("/minions/web-01/states/refresh")
    assert rv.status_code == 200
    assert seen["args"] == ("web-01",)
    assert seen["kwargs"].get("user") == "seer"


def test_single_minion_run_visible_to_lead(env):
    login_as(env, "teamop")
    rv = env.post(
        "/jobs/run",
        data={"tgt": "web-01", "tgt_type": "list", "fun": "test.ping", "mode": "async"},
    )
    assert rv.status_code == 302
    with env.app.app_context():
        row = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "run:test.ping")
            .one()
        )
        assert row.minion_id == "web-01"
        assert row.user == "teamop"
    login_as(env, "lead")
    html = env.get("/audit/").data.decode()
    assert "run:test.ping" in html

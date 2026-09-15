"""Job-truth tests: synthetic JIDs never age out, sync/ssh launches persist
returns, unique returns, and master-gated sticky completion."""

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs_service import sync_job
from overstate_ui.models import Job, JobReturn, Minion, SaltReturn


@pytest.fixture()
def client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        for mid in ("web-01", "web-02"):
            get_session().add(Minion(id=mid, grains={}, conformity={"status": "ok"}))
        get_session().commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def _old_job(session, jid, fun="state.apply", tgt_type="glob", minutes=2):
    session.add(
        Job(
            jid=jid,
            fun=fun,
            tgt="*",
            tgt_type=tgt_type,
            user="admin",
            complete=False,
        )
    )
    session.commit()
    job = session.get(Job, jid)
    job.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)
    session.commit()
    return job


def test_batch_parent_never_ages_out(client):
    with client.app.app_context():
        session = get_session()
        _old_job(session, "batch-abc", fun="state.apply")
        assert sync_job("batch-abc").complete is False
        for mid in ("web-01", "web-02"):
            assert session.get(Minion, mid).conformity["status"] == "ok"


def test_orch_job_never_ages_out(client):
    with client.app.app_context():
        session = get_session()
        _old_job(session, "orch-xyz", fun="state.orchestrate", tgt_type="runner")
        assert sync_job("orch-xyz").complete is False


def test_real_jid_still_ages_out(client):
    with client.app.app_context():
        _old_job(get_session(), "202609150000000099", fun="test.ping")
        assert sync_job("202609150000000099").complete is True


class _StubSalt:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def local(self, tgt, fun, **kwargs):
        self.calls.append((tgt, fun, kwargs))
        return self.result


def test_sync_launch_persists_returns(client, monkeypatch):
    from overstate_ui import jobs_service

    stub = _StubSalt([{"web-01": True}])
    monkeypatch.setattr(jobs_service, "get_salt", lambda: stub)
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "sync",
            "via": "local",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert jid.startswith("sync-")
    assert stub.calls[0][2]["http_timeout"] >= 60
    with client.app.app_context():
        rows = get_session().query(JobReturn).filter_by(jid=jid).all()
        assert [r.minion_id for r in rows] == ["web-01"]
        assert get_session().get(Job, jid).complete is True
    html = client.get(f"/jobs/{jid}").data.decode()
    assert "web-01" in html
    assert "No returns recorded" not in html


def test_ssh_launch_persists_returns(client, monkeypatch):
    from overstate_ui import jobs_service

    stub = _StubSalt([{"web-01": True}])
    monkeypatch.setattr(jobs_service, "get_salt", lambda: stub)
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "sync",
            "via": "ssh",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert jid.startswith("ssh-")
    assert stub.calls[0][2]["http_timeout"] >= 180
    with client.app.app_context():
        rows = get_session().query(JobReturn).filter_by(jid=jid).all()
        assert [r.minion_id for r in rows] == ["web-01"]
    html = client.get(f"/jobs/{jid}").data.decode()
    assert "web-01" in html
    assert "No returns recorded" not in html


def test_sync_launch_keeps_real_jid(client, monkeypatch):
    from overstate_ui import jobs_service

    stub = _StubSalt([{"jid": "202609150000000001", "web-01": True}])
    monkeypatch.setattr(jobs_service, "get_salt", lambda: stub)
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "sync",
            "via": "local",
        },
    )
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/jobs/202609150000000001")
    with client.app.app_context():
        session = get_session()
        assert session.get(Job, "202609150000000001").complete is True
        rows = session.query(JobReturn).filter_by(jid="202609150000000001").all()
        assert [r.minion_id for r in rows] == ["web-01"]


def test_job_returns_unique_pair(client):
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609150000000002",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="202609150000000002",
                minion_id="web-01",
                success=True,
                retcode=0,
                payload={},
            )
        )
        session.commit()
        session.add(
            JobReturn(
                jid="202609150000000002",
                minion_id="web-01",
                success=True,
                retcode=0,
                payload={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def _returner_row(jid, mid, minutes_old=0):
    return SaltReturn(
        fun="test.ping",
        jid=jid,
        minion_id=mid,
        success="True",
        payload={"result": True},
        full_ret={},
        alter_time=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_old),
    )


class _QuietMaster:
    def runner(self, fun, **kwargs):
        return [{}]


class _ActiveMaster:
    def __init__(self, jid, mids):
        self.jid = jid
        self.mids = mids

    def runner(self, fun, **kwargs):
        assert fun == "jobs.lookup_jid"
        return [{self.jid: {mid: {"result": True} for mid in self.mids}}]


def test_unrelated_runner_payload_falls_back(client, monkeypatch):
    from overstate_ui import jobs_service

    class _StatusMaster:
        def runner(self, fun, **kwargs):
            return [{"up": [], "down": []}]

    monkeypatch.setattr(jobs_service, "get_salt", lambda: _StatusMaster())
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609150000000007",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.add(_returner_row("202609150000000007", "web-01", minutes_old=2))
        session.commit()
        assert sync_job("202609150000000007").complete is True


def test_sync_upserts_without_duplicates(client, monkeypatch):
    from overstate_ui import jobs_service

    monkeypatch.setattr(jobs_service, "get_salt", lambda: _QuietMaster())
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609150000000003",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.add(_returner_row("202609150000000003", "web-01"))
        session.commit()
        sync_job("202609150000000003")
        sync_job("202609150000000003")
        rows = session.query(JobReturn).filter_by(jid="202609150000000003").all()
        assert [r.minion_id for r in rows] == ["web-01"]


def test_master_active_blocks_completion(client, monkeypatch):
    from overstate_ui import jobs_service

    jid = "202609150000000004"
    monkeypatch.setattr(
        jobs_service, "get_salt", lambda: _ActiveMaster(jid, ["web-01", "web-02"])
    )
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid=jid,
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        session.add(_returner_row(jid, "web-01", minutes_old=2))
        session.commit()
        assert sync_job(jid).complete is False


def test_complete_is_sticky_without_new_minions(client, monkeypatch):
    from overstate_ui import jobs_service

    monkeypatch.setattr(jobs_service, "get_salt", lambda: _QuietMaster())
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609150000000005",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        session.add(_returner_row("202609150000000005", "web-01"))
        session.add(
            JobReturn(
                jid="202609150000000005",
                minion_id="web-01",
                success=True,
                retcode=0,
                payload={},
            )
        )
        session.commit()
        assert sync_job("202609150000000005").complete is True


def test_new_minion_reopens_completed_job(client, monkeypatch):
    from overstate_ui import jobs_service

    monkeypatch.setattr(jobs_service, "get_salt", lambda: _QuietMaster())
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="202609150000000006",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="202609150000000006",
                minion_id="web-01",
                success=True,
                retcode=0,
                payload={},
            )
        )
        session.add(_returner_row("202609150000000006", "web-02"))
        session.commit()
        assert sync_job("202609150000000006").complete is False
        rows = session.query(JobReturn).filter_by(jid="202609150000000006").all()
        assert sorted(r.minion_id for r in rows) == ["web-01", "web-02"]

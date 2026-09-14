"""Batch execution tests: splitting, gates, cancel, views."""

import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn, Minion


@pytest.fixture()
def app():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["REDIS_URL"] = "redis://127.0.0.1:9/0"
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        for mid in ("web-01", "web-02", "db-01"):
            get_session().add(Minion(id=mid, grains={}, conformity={}))
        get_session().commit()
    return app


@pytest.fixture()
def admin(app):
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return client


def test_invalid_batch_config_does_not_fire(admin, app, monkeypatch):
    import overstate_ui.jobs_service as jobs_service

    fired = []

    class FireSalt:
        def local(self, *a, **k):
            fired.append(True)
            return [{"jid": "w9"}]

    monkeypatch.setattr(jobs_service, "get_salt", lambda: FireSalt())
    with app.app_context():
        before = get_session().query(Job).count()
    rv = admin.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "async",
            "via": "local",
            "batch_mode": "count",
            "batch_size": "0",
            "stop_after": "1",
        },
    )
    assert rv.status_code == 302
    assert "/jobs/detail" not in rv.headers["Location"]
    assert "/jobs/batch-" not in rv.headers["Location"]
    assert fired == []
    with app.app_context():
        assert get_session().query(Job).count() == before
    assert "Batch" in admin.get(rv.headers["Location"]).data.decode()


class StubSalt:
    def __init__(self):
        self.published = []

    def local(self, tgt, fun, **kwargs):
        self.published.append(list(tgt) if isinstance(tgt, list) else tgt)
        return [{"jid": f"w{len(self.published)}"}]


def seed_returns(jid, results):
    session = get_session()
    for mid, success in results:
        session.add(
            JobReturn(jid=jid, minion_id=mid, success=success, retcode=0, payload={})
        )
    session.commit()


def parent_row(group):
    return get_session().get(Job, f"batch-{group}")


def test_split_roster_count_percent_remainder():
    from overstate_ui.tasks import split_roster

    roster = ["a", "b", "c", "d", "e"]
    assert split_roster(roster, "count", 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert split_roster(roster, "percent", 40) == [["a", "b"], ["c", "d"], ["e"]]
    assert split_roster(roster, "percent", 100) == [roster]
    assert split_roster([], "count", 2) == []
    assert split_roster(roster, "count", 0) == [[m] for m in roster]


def test_parse_batch_fields():
    from overstate_ui.jobs import parse_batch_fields

    assert parse_batch_fields({}) is None
    assert parse_batch_fields({"batch_mode": "off"}) is None
    assert (
        parse_batch_fields(
            {"batch_mode": "count", "batch_size": "x", "stop_after": "1"}
        )
        is None
    )
    assert (
        parse_batch_fields(
            {"batch_mode": "count", "batch_size": "2", "stop_after": "0"}
        )
        is None
    )
    assert parse_batch_fields(
        {"batch_mode": "percent", "batch_size": "25", "stop_after": "3"}
    ) == {"mode": "percent", "size": 25, "stop_after": 3}


def test_resolve_batch_roster(app):
    from overstate_ui.jobs import resolve_batch_roster

    with app.app_context():
        assert resolve_batch_roster("web-*", "glob") == ["web-01", "web-02"]
        assert resolve_batch_roster("web-01,db-01", "list") == ["db-01", "web-01"]
        assert resolve_batch_roster("nope-*", "glob") == []
        assert resolve_batch_roster("x", "grain") is None


def test_gate_trips_and_stops(monkeypatch, app):
    import overstate_ui.tasks as tasks_mod
    from overstate_ui.tasks import run_wave_batch

    stub = StubSalt()
    monkeypatch.setattr(tasks_mod, "build_client", lambda: stub)
    with app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="batch-g1",
                fun="test.ping",
                tgt="web-*",
                tgt_type="glob",
                user="admin",
                batch_group="g1",
                batch_state={"status": "running"},
            )
        )
        session.commit()
        seed_returns("w1", [("web-01", False)])
        out = run_wave_batch(
            "g1", [["web-01"], ["web-02"]], "test.ping", [], 1, "admin", wave_timeout=0
        )
    assert out == {"group": "g1", "status": "stopped", "failures": 1, "waves": 2}
    assert stub.published == [["web-01"]]  # second wave never published
    with app.app_context():
        assert parent_row("g1").complete is True
        assert parent_row("g1").batch_state["status"] == "stopped"


def test_complete_batch_and_audit(monkeypatch, app):
    import overstate_ui.tasks as tasks_mod
    from overstate_ui.models import AuditEvent
    from overstate_ui.tasks import run_wave_batch

    stub = StubSalt()
    monkeypatch.setattr(tasks_mod, "build_client", lambda: stub)
    with app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="batch-g2",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                batch_group="g2",
                batch_state={"status": "running"},
            )
        )
        session.commit()
        seed_returns("w1", [("web-01", True)])
        seed_returns("w2", [("web-02", True), ("db-01", True)])
        out = run_wave_batch(
            "g2",
            [["web-01"], ["web-02", "db-01"]],
            "test.ping",
            [],
            1,
            "admin",
            wave_timeout=0,
        )
    assert out["status"] == "complete" and out["failures"] == 0
    assert len(stub.published) == 2
    with app.app_context():
        actions = [
            e.action
            for e in get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action.like("batch-%"))
            .all()
        ]
        assert "batch-start:g2" not in actions  # view writes that
        assert "batch-complete:g2" in actions
        assert "batch-wave:g2:1" in actions


def test_cancel_stops_before_first_wave(monkeypatch, app):
    import overstate_ui.tasks as tasks_mod
    from overstate_ui.tasks import run_wave_batch

    stub = StubSalt()
    monkeypatch.setattr(tasks_mod, "build_client", lambda: stub)
    monkeypatch.setattr(tasks_mod, "batch_cancelled", lambda group: True)
    with app.app_context():
        get_session().add(
            Job(
                jid="batch-g3",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                batch_group="g3",
                batch_state={"status": "running"},
            )
        )
        get_session().commit()
        out = run_wave_batch(
            "g3", [["web-01"]], "test.ping", [], 1, "admin", wave_timeout=0
        )
    assert out["status"] == "cancelled"
    assert stub.published == []


def test_batched_run_view_inline(monkeypatch, app, admin):
    import overstate_ui.tasks as tasks_mod

    stub = StubSalt()
    monkeypatch.setattr(tasks_mod, "build_client", lambda: stub)
    with app.app_context():
        seed_returns("w1", [("web-01", True)])
        seed_returns("w2", [("web-02", True)])
    rv = admin.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "async",
            "via": "local",
            "batch_mode": "count",
            "batch_size": "1",
            "stop_after": "1",
        },
    )
    assert rv.status_code == 302
    assert "/jobs/batch-" in rv.headers["Location"]
    group = rv.headers["Location"].rsplit("batch-", 1)[1]
    assert stub.published == [["web-01"], ["web-02"]]
    html = admin.get(f"/jobs/batch-{group}").data.decode()
    assert "Batch" in html and "complete" in html
    assert "Stop batch" not in html  # finished batches offer no cancel


def test_batch_parent_page_aggregates_wave_returns(monkeypatch, app, admin):
    """The batch parent row is a grouping record Salt never ran: its
    page must aggregate the wave returns instead of showing a complete
    batch above a permanent 'No returns yet'."""
    import overstate_ui.tasks as tasks_mod

    stub = StubSalt()
    monkeypatch.setattr(tasks_mod, "build_client", lambda: stub)
    with app.app_context():
        seed_returns("w1", [("web-01", True)])
        seed_returns("w2", [("web-02", False)])
    rv = admin.post(
        "/jobs/run",
        data={
            "tgt": "web-*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "mode": "async",
            "via": "local",
            "batch_mode": "count",
            "batch_size": "1",
            "stop_after": "1",
        },
    )
    group = rv.headers["Location"].rsplit("batch-", 1)[1]
    html = admin.get(f"/jobs/batch-{group}").data.decode()
    assert "No returns yet" not in html
    assert "web-01" in html and "web-02" in html
    assert ">2</span> returned" in html and ">1</span> failed" in html


def test_batched_run_rejects_ungroupable_target(admin):
    rv = admin.post(
        "/jobs/run",
        data={
            "tgt": "role:web",
            "tgt_type": "compound",
            "fun": "test.ping",
            "mode": "async",
            "via": "local",
            "batch_mode": "count",
            "batch_size": "1",
            "stop_after": "1",
        },
        follow_redirects=True,
    )
    assert "Batch mode supports list, glob, and group targets." in rv.data.decode()


def test_cancel_route_sets_flag(monkeypatch, admin):
    import overstate_ui.tasks as tasks_mod

    seen = {}
    monkeypatch.setattr(
        tasks_mod,
        "request_batch_cancel",
        lambda group: seen.setdefault("group", group) or True,
    )
    rv = admin.post("/jobs/batch/g9/cancel")
    assert rv.status_code == 302
    assert seen["group"] == "g9"

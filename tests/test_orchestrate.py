"""Orchestrate tests: validation, launch, return storage."""

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Job, JobReturn


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["REDIS_URL"] = "redis://127.0.0.1:9/0"
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def test_orchestrate_page_renders():
    html = make_client().get("/jobs/orchestrate").data.decode()
    assert "Orchestrate" in html
    assert 'name="mods"' in html and 'name="pillar"' in html


def test_orchestrate_rejects_bad_input():
    c = make_client()
    rv = c.post(
        "/jobs/orchestrate/run", data={"mods": "evil; rm"}, follow_redirects=True
    )
    assert "dotted orchestration name" in rv.data.decode()
    rv = c.post(
        "/jobs/orchestrate/run",
        data={"mods": "orch.ok", "pillar": "{nope"},
        follow_redirects=True,
    )
    assert "not valid JSON" in rv.data.decode()
    rv = c.post(
        "/jobs/orchestrate/run",
        data={"mods": "orch.ok", "pillar": "[1, 2]"},
        follow_redirects=True,
    )
    assert "must be a JSON object" in rv.data.decode()


def test_orchestrate_runs_and_stores_returns(monkeypatch):
    import overstate_ui.tasks as tasks_mod

    seen = {}

    class StubRunner:
        def runner(self, fun, **kwargs):
            seen.update(fun=fun, kwargs=kwargs)
            return [
                {
                    "web-01": {"result": True, "comment": "ok"},
                    "web-02": {"result": False, "comment": "bad"},
                }
            ]

    monkeypatch.setattr(tasks_mod, "build_client", lambda: StubRunner())
    c = make_client()
    rv = c.post(
        "/jobs/orchestrate/run",
        data={"mods": "orch.demo", "saltenv": "base", "pillar": '{"role": "web"}'},
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert jid.startswith("orch-")
    assert seen["fun"] == "state.orchestrate"
    assert seen["kwargs"]["mods"] == "orch.demo"
    assert seen["kwargs"]["pillar"] == {"role": "web"}
    assert seen["kwargs"]["test"] is False
    with c.application.app_context():
        job = get_session().get(Job, jid)
        assert job.complete is True
        rows = {
            r.minion_id: r.success
            for r in get_session().query(JobReturn).filter_by(jid=jid).all()
        }
        assert rows == {"web-01": True, "web-02": False}
    html = c.get(f"/jobs/{jid}").data.decode()
    assert "state.orchestrate" in html


def test_orch_success_scan():
    from overstate_ui.tasks import _orch_success

    assert _orch_success({"a": {"result": True}}) is True
    assert _orch_success({"a": {"result": False}}) is False
    assert _orch_success({"a": [{"result": True}, {"result": False}]}) is False
    assert _orch_success({"output": "text"}) is True

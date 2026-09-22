"""Phase 5 tests: run, sync from returner rows, history, saved, SSE stream."""

import datetime as dt
import json

import httpx
import pytest

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.jobs import sync_job
from overstate_ui.models import AuditEvent, Job, JobReturn, SavedJob
from overstate_ui.salt_client import SaltClient
from overstate_ui.seed_mock import seed as seed_mock


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "local_async":
            assert body["fun"] == "test.ping"
            return httpx.Response(
                200, json={"return": [{"jid": "99999", "minions": ["m1"]}]}
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
        seed_mock(get_session())
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    c.app = app
    return c


def test_run_async_creates_job_and_audit(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "async",
            "save_as": "ping all",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert len(jid) == 20 and jid.isdigit()  # app-side shared JID (D12)
    with client.app.app_context():
        job = get_session().get(Job, jid)
        assert job is not None and job.fun == "test.ping"
        assert job.complete is False
        audit = (
            get_session()
            .query(AuditEvent)
            .filter(AuditEvent.action == "run:test.ping")
            .one()
        )
        assert audit.jid == jid
        saved = get_session().query(SavedJob).filter_by(name="ping all").one()
        assert saved.fun == "test.ping"


def test_run_rejects_disallowed_function(client):
    rv = client.post(
        "/jobs/run",
        data={"tgt": "*", "tgt_type": "glob", "fun": "cmd.run", "args": "id"},
        follow_redirects=True,
    )
    assert "cannot run here" in rv.data.decode()
    with client.app.app_context():
        assert get_session().query(Job).filter_by(fun="cmd.run").count() == 0


def test_run_rejects_scheduled_smuggled_function(client):
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "web-01",
            "tgt_type": "list",
            "fun": "schedule.add",
            "args": "nightly function=cmd.run seconds=60",
        },
        follow_redirects=True,
    )
    assert "cannot run here" in rv.data.decode()
    with client.app.app_context():
        assert get_session().query(Job).filter_by(fun="schedule.add").count() == 0


def test_fun_doc_rejects_glob_minion(client):
    assert client.get("/jobs/fun-doc?fun=test.ping&minion=*").status_code == 400
    assert client.get("/jobs/fun-doc?fun=test.ping&minion=web-*").status_code == 400


def test_run_requires_fun(client):
    rv = client.post("/jobs/run", data={"tgt": "*", "tgt_type": "glob", "fun": ""})
    assert rv.status_code == 302
    assert "new" in rv.headers["Location"]


def test_delete_saved_unknown_id_flashes(client):
    rv = client.post("/jobs/saved/999999/delete")
    assert rv.status_code == 302
    assert "No such saved job." in client.get(rv.headers["Location"]).data.decode()


def test_sync_copies_returner_rows(client):
    with client.app.app_context():
        job = sync_job("20260910123000000002")
        assert job is not None
        assert job.fun == "state.highstate"
        returns = (
            get_session().query(JobReturn).filter_by(jid="20260910123000000002").all()
        )
        assert len(returns) == 2
        failed = [r for r in returns if not r.success]
        assert len(failed) == 1 and failed[0].minion_id == "tw-minion-02"


def test_sync_completes_old_job_without_returns(client):
    with client.app.app_context():
        from overstate_ui.models import Job

        get_session().add(
            Job(
                jid="42424242424242424242",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().add(
            Job(
                jid="42424242424242424243",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                complete=False,
            )
        )
        get_session().commit()
        old = get_session().get(Job, "42424242424242424242")
        old.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        get_session().commit()
        assert sync_job("42424242424242424242").complete is True
        assert sync_job("42424242424242424243").complete is False


def test_detail_and_history_render(client):
    with client.app.app_context():
        sync_job("20260910123000000002")
    html = client.get("/jobs/20260910123000000002").data.decode()
    assert "tw-minion-02" in html and "failed" in html
    assert "Sync results" in html


def test_detail_recovery_links(client):
    from overstate_ui.models import AuditEvent

    with client.app.app_context():
        sync_job("20260910123000000002")
        get_session().add(
            AuditEvent(user="admin", action="kill:20260910123000000002", jid="kk")
        )
        get_session().commit()
    html = client.get("/jobs/20260910123000000002").data.decode()
    assert html.count("Re-run") == 1
    assert "tgt=tw-minion-02" in html and "tgt_type=list" in html
    assert "Failed states:" in html
    assert "pkg_|-nginx_|-nginx_|-installed" in html
    assert "check presence" in html


def test_detail_counts_real_state_payload(client):
    """Regression: real state returns carry no succeeded/failed keys —
    the page must count per-state results instead of showing '?'."""
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000009",
                fun="state.apply",
                tgt="web01",
                tgt_type="list",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="20260910123000000009",
                minion_id="web01",
                success=False,
                retcode=1,
                payload={
                    "web_|-pkg_|-nginx_|-installed": {
                        "result": True,
                        "changes": {},
                    },
                    "web_|-service_|-nginx_|-running": {
                        "result": False,
                        "changes": {},
                    },
                    "file_|-motd_|-/etc/motd_|-managed": {
                        "result": True,
                        "changes": {},
                    },
                },
            )
        )
        session.commit()
    html = client.get("/jobs/20260910123000000009").data.decode()
    assert "2 succeeded, 1 failed" in html
    assert "? succeeded" not in html


def test_summarize_state_return_counts_results():
    from overstate_ui.jobs_helpers import summarize_state_return

    assert summarize_state_return(True) is None
    assert summarize_state_return("ok") is None
    assert summarize_state_return({}) is None
    assert summarize_state_return({"outputter": "highstate"}) is None
    assert summarize_state_return({"succeeded": 42, "failed": 0}) == {
        "succeeded": 42,
        "failed": 0,
    }
    assert summarize_state_return(
        {
            "a_|-b_|-c_|-d": {"result": True},
            "e_|-f_|-g_|-h": {"result": False},
            "i_|-j_|-k_|-l": {"result": None},
            "not-a-state": {"changes": {}},
        }
    ) == {"succeeded": 2, "failed": 1}


def test_describe_return_shapes():
    from overstate_ui.jobs_helpers import describe_return

    state = describe_return(
        {
            "web_|-service_|-nginx_|-running": {
                "result": False,
                "comment": "Failed!\nsecond line",
                "duration": 12.0,
                "changes": {},
            },
            "web_|-pkg_|-nginx_|-installed": {
                "result": True,
                "comment": "All good",
                "duration": 3.0,
                "changes": {"installed": ["x"]},
            },
            "odd": {"changes": {}},
        }
    )
    assert state["kind"] == "state"
    assert state["summary"] == {"succeeded": 1, "failed": 1}
    assert [(s["module"], s["name"]) for s in state["states"]] == [
        ("service", "nginx"),
        ("pkg", "nginx"),
    ]  # failures first
    assert state["states"][0]["comment"] == "Failed!"  # first line only
    assert state["states"][0]["changes"] is None  # empty changes fade out
    assert state["states"][1]["changes"] == {"installed": ["x"]}

    assert describe_return(True)["text"] == "Returned True"
    assert describe_return("hello")["kind"] == "scalar"
    assert describe_return({"a": 1})["kind"] == "unknown"
    assert describe_return([True, "x"])["text"] == "Returned 2 items: True, x"
    assert describe_return([{"a": 1}])["kind"] == "unknown"
    seed = describe_return({"succeeded": 1, "failed": 0, "failures": ["a", "b"]})
    assert seed["kind"] == "summary" and seed["failures"] == ["a", "b"]


def test_detail_renders_human_state_rows(client):
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000010",
                fun="state.apply",
                tgt="web01",
                tgt_type="list",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="20260910123000000010",
                minion_id="web01",
                success=False,
                retcode=1,
                payload={
                    "web_|-service_|-nginx_|-running": {
                        "result": False,
                        "comment": "Service failed to start",
                        "duration": 42.0,
                        "changes": {},
                    },
                    "web_|-pkg_|-nginx_|-installed": {
                        "result": True,
                        "comment": "Already installed",
                        "changes": {"x": 1},
                    },
                },
            )
        )
        session.commit()
    html = client.get("/jobs/20260910123000000010").data.decode()
    assert "service: nginx" in html  # human row, failed first
    assert html.index("service: nginx") < html.index("pkg: nginx")
    assert "Service failed to start" in html
    assert "42.0 ms" in html
    assert "Raw output" in html  # full JSON one click away


def _seed_panel_job(client):
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000012",
                fun="test.ping",
                tgt="web01",
                tgt_type="list",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="20260910123000000012",
                minion_id="web01",
                success=True,
                retcode=0,
                payload=True,
            )
        )
        session.commit()


def test_stream_lists_returned_minions(client):
    _seed_panel_job(client)
    rv = client.get("/jobs/20260910123000000012/stream?interval=0.05")
    text = rv.data.decode()
    assert '"minions": ["web01"]' in text
    assert "event: done" in text


def test_panel_fragment_renders_one_minion(client):
    _seed_panel_job(client)
    rv = client.get("/jobs/20260910123000000012/panel/web01")
    assert rv.status_code == 200
    assert 'data-panel-mid="web01"' in rv.data.decode()
    assert "Returned True" in rv.data.decode()


def test_panel_fragment_404s(client):
    _seed_panel_job(client)
    assert client.get("/jobs/nope/panel/web01").status_code == 404
    assert client.get("/jobs/20260910123000000012/panel/ghost").status_code == 404


def test_detail_carries_live_panel_container(client):
    _seed_panel_job(client)
    html = client.get("/jobs/20260910123000000012").data.decode()
    assert 'id="return-panels"' in html
    assert "data-panel-url" in html
    assert 'data-panel-mid="web01"' in html


def test_running_detail_reconnects_stream(client):
    import datetime as dt

    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000013",
                fun="test.ping",
                tgt="web01",
                tgt_type="list",
                user="admin",
                started_at=dt.datetime.now(dt.UTC),
                complete=False,
            )
        )
        session.commit()
    html = client.get("/jobs/20260910123000000013").data.decode()
    assert "EventSource" in html
    # Capped connections reopen; the note tracks reconnect state.
    assert "Reconnecting" in html
    assert "setTimeout(connect" in html
    assert "var reconnecting = false" in html
    assert "completeSeen || reconnecting" in html
    assert "src.onopen" in html
    assert "missing[mid] = true" not in html
    assert "pending[mid]" in html
    assert "if (panels.querySelector(sel)) return;" in html
    assert "Session expired. Reload." in html


def test_describe_return_failure_surface():
    from overstate_ui.jobs_helpers import describe_return

    view = describe_return(
        {
            "pkg_|-schedule_croniter_package_|-python3-croniter_|-installed": {
                "__id__": "schedule_croniter_package",
                "__sls__": "baseline.schedule",
                "__run_num__": 3,
                "result": False,
                "comment": "An error was encountered\nPackage 'python3-croniter' not found.",
                "duration": 25907.581,
                "changes": {},
            },
            "file_|-motd_banner_|-/etc/motd_|-managed": {
                "__id__": "motd_banner",
                "__sls__": "baseline.banner",
                "__run_num__": 1,
                "result": True,
                "comment": "File is in the correct state",
                "duration": 96.63,
                "changes": {},
            },
            "file_|-resolved_runtime_dir_|-/run/systemd/resolve_|-directory": {
                "__id__": "resolved_runtime_dir",
                "__sls__": "baseline.systemd-resolved",
                "__run_num__": 7,
                "result": True,
                "comment": "Directory updated",
                "duration": 8.81,
                "changes": {"/run/systemd/resolve": {"user": "root"}},
            },
        }
    )
    assert view["kind"] == "state"
    assert view["summary"] == {"succeeded": 2, "failed": 1}  # shape frozen
    assert view["changed"] == 1
    assert view["failed_names"] == ["schedule_croniter_package: python3-croniter"]
    states = view["states"]
    assert [s["label"] for s in states] == [
        "schedule_croniter_package: python3-croniter",  # failed first
        "resolved_runtime_dir: /run/systemd/resolve",  # changed next
        "motd_banner: /etc/motd",  # quiet ok last
    ]
    failed = states[0]
    assert failed["sls"] == "baseline.schedule"
    assert failed["comment"] == "An error was encountered"  # first line kept
    assert "not found" in failed["comment_full"]  # full reason kept
    assert failed["duration_text"] == "25.9 s"  # scaled, not raw ms
    assert states[2]["duration_text"] == "96.63 ms"


def test_describe_return_failed_names_capped():
    from overstate_ui.jobs_helpers import describe_return

    payload = {
        f"mod_|-id{i}_|-n{i}_|-fun": {"result": False, "comment": "bad"}
        for i in range(6)
    }
    view = describe_return(payload)
    assert view["failed_names"] == ["id0: n0", "id1: n1", "id2: n2", "+3 more"]


def test_detail_shows_failed_names_and_full_reason(client):
    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000011",
                fun="state.highstate",
                tgt="web01",
                tgt_type="list",
                user="admin",
                complete=True,
            )
        )
        session.add(
            JobReturn(
                jid="20260910123000000011",
                minion_id="web01",
                success=False,
                retcode=1,
                payload={
                    "pkg_|-croniter_|-python3-croniter_|-installed": {
                        "__id__": "croniter",
                        "__sls__": "baseline.schedule",
                        "result": False,
                        "comment": "An error was encountered\nPackage not found.",
                        "duration": 5.0,
                        "changes": {},
                    },
                },
            )
        )
        session.commit()
    html = client.get("/jobs/20260910123000000011").data.decode()
    assert "Failed: croniter: python3-croniter" in html  # scan-level names
    assert "Package not found." in html  # full reason, no raw open needed
    assert "baseline.schedule" in html  # sls badge
    assert "Raw output" in html


def test_new_prefills_fun_and_args(client):
    html = client.get(
        "/jobs/new?tgt=x&tgt_type=glob&fun=test.ping&args=a"
    ).data.decode()
    assert 'name="fun" value="test.ping"' in html
    assert 'name="args" value="a"' in html


def test_jobs_history_search_filters_rows(client):
    html = client.get("/jobs/?tab=history&q=highstate").data.decode()
    assert "20260910123000000002" in html  # highstate row stays
    assert "20260910120000000001" not in html  # ping row filtered out
    plain = client.get("/jobs/?tab=history").data.decode()
    assert "20260910120000000001" in plain
    assert "20260910123000000002" in plain


def test_jobs_page_pause_labels_scope(client):
    html = client.get("/jobs/").data.decode()
    assert "Pause live updates" in html
    # The toggle pauses the job and minion lists only.
    assert "job and minion lists" in html
    assert "all pages" not in html


def test_stream_completes_for_old_job(client):
    with client.app.app_context():
        from overstate_ui.models import SaltReturn

        job = sync_job("20260910123000000002")
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        job.started_at = old
        for sr in get_session().query(SaltReturn).all():
            sr.alter_time = old
        get_session().commit()
    rv = client.get("/jobs/20260910123000000002/stream?interval=0.05")
    text = rv.data.decode()
    assert '"complete": true' in text
    assert "event: done" in text


def test_saved_delete(client):
    with client.app.app_context():
        saved_id = get_session().query(SavedJob).first().id
    rv = client.post(f"/jobs/saved/{saved_id}/delete")
    assert rv.status_code == 302
    with client.app.app_context():
        assert get_session().query(SavedJob).count() == 1


def test_new_renders_operation_library(client):
    from overstate_ui.jobs import OPERATION_GROUPS

    html = client.get("/jobs/new").data.decode()
    assert 'id="op-search"' in html
    assert 'id="fun-list"' in html
    assert 'id="blast"' not in html
    assert "os:Debian" in html
    for group, ops in OPERATION_GROUPS:
        assert group.replace("&", "&amp;") in html
        for op in ops:
            assert f"/jobs/new?preset={op['preset']}" in html
            assert op["fun"] in html
            assert op["about"] in html


def test_new_marks_active_preset(client):
    html = client.get("/jobs/new?preset=ping").data.decode()
    assert 'aria-current="true"' in html
    assert 'value="test.ping"' in html


def test_library_collapsed_by_default(client):
    html = client.get("/jobs/new").data.decode()
    assert 'id="op-library-toggle"' in html
    assert "checked" not in html
    html = client.get("/jobs/new?preset=ping").data.decode()
    assert 'id="op-library-toggle"' in html
    assert "checked" in html


def test_docs_slot_lives_in_rail_after_form(client):
    html = client.get("/jobs/new").data.decode()
    assert "<aside" in html
    assert 'id="fun-doc"' in html
    assert html.index('id="fun-doc"') > html.index("</form>")
    assert 'id="job-fun"' in html
    assert "Loading docs" in html  # fetch loading line in script


def test_run_job_links_show_loading_state(client):
    # jobs/new waits on the Salt API; every entry point must give
    # instant feedback instead of looking dead.
    html = client.get("/jobs/").data.decode()
    assert html.count("data-loading-link") >= 2  # navbar + page button
    assert "loading loading-spinner" in html  # base loading script
    assert "Loading…" in html


def test_run_form_shows_firing_state(client):
    html = client.get("/jobs/new").data.decode()
    assert "data-loading-form" in html
    assert "Firing…" in html


def test_duplicate_save_as_still_runs(client):
    with client.app.app_context():
        get_session().add(
            SavedJob(
                name="ping",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                args=[],
            )
        )
        get_session().commit()
    rv = client.post(
        "/jobs/run",
        data={
            "tgt": "*",
            "tgt_type": "glob",
            "fun": "test.ping",
            "args": "",
            "mode": "async",
            "save_as": "ping",
        },
    )
    assert rv.status_code == 302
    jid = rv.headers["Location"].rsplit("/", 1)[1]
    assert len(jid) == 20 and jid.isdigit()  # app-side shared JID (D12)
    html = client.get(rv.headers["Location"]).data.decode()
    assert "Name taken. The job still ran." in html


def test_stream_counts_live_cache_minions(client, monkeypatch):
    from types import SimpleNamespace

    import overstate_ui.jobs as jobsmod

    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000020",
                fun="test.ping",
                tgt="web01",
                tgt_type="list",
                user="admin",
                started_at=dt.datetime.now(dt.UTC),
                complete=False,
            )
        )
        session.commit()
    live = [
        SimpleNamespace(
            minion_id="web01", success=True, retcode=0, payload=True, live=True
        )
    ]
    monkeypatch.setattr(jobsmod, "live_returns_now", lambda client, jid: live)
    monkeypatch.setattr(jobsmod, "sync_job", lambda jid: get_session().get(Job, jid))
    rv = client.get("/jobs/20260910123000000020/stream?interval=0.05")
    first = rv.data.decode().splitlines()[0]
    payload = json.loads(first.removeprefix("data: "))
    assert payload["minions"] == ["web01"]
    assert payload["returned"] == 1
    assert payload["live"] == 1
    assert payload["stored"] == 0


def test_stream_sends_live_and_failed_counts(client, monkeypatch):
    from types import SimpleNamespace

    import overstate_ui.jobs as jobsmod

    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="20260910123000000021",
                fun="test.ping",
                tgt="*",
                tgt_type="glob",
                user="admin",
                started_at=dt.datetime.now(dt.UTC),
                complete=False,
            )
        )
        session.add(
            JobReturn(
                jid="20260910123000000021",
                minion_id="stored01",
                success=False,
                retcode=1,
                payload=False,
            )
        )
        session.commit()
    live = [
        SimpleNamespace(
            minion_id="live01", success=False, retcode=1, payload=False, live=True
        )
    ]
    monkeypatch.setattr(jobsmod, "live_returns_now", lambda client, jid: live)
    monkeypatch.setattr(jobsmod, "sync_job", lambda jid: get_session().get(Job, jid))
    rv = client.get("/jobs/20260910123000000021/stream?interval=0.05")
    first = rv.data.decode().splitlines()[0]
    payload = json.loads(first.removeprefix("data: "))
    assert sorted(payload["minions"]) == ["live01", "stored01"]
    assert payload["returned"] == 2
    assert payload["failed"] == 2
    assert payload["live"] == 1
    assert payload["stored"] == 1


def test_batch_parent_stream_unions_wave_live_rows(client, monkeypatch):
    from types import SimpleNamespace

    import overstate_ui.jobs as jobsmod

    with client.app.app_context():
        session = get_session()
        session.add(
            Job(
                jid="batch-g1",
                fun="state.apply",
                tgt="*",
                tgt_type="glob",
                user="admin",
                batch_group="g1",
                started_at=dt.datetime.now(dt.UTC),
                complete=False,
            )
        )
        session.add(
            Job(
                jid="20260910123000000030",
                fun="state.apply",
                tgt="web01",
                tgt_type="list",
                user="admin",
                batch_group="g1",
                started_at=dt.datetime.now(dt.UTC),
                complete=False,
            )
        )
        session.commit()

    def fake_live(client, jid):
        if jid == "20260910123000000030":
            return [
                SimpleNamespace(
                    minion_id="web01",
                    success=True,
                    retcode=0,
                    payload=True,
                    live=True,
                )
            ]
        return []

    monkeypatch.setattr(jobsmod, "live_returns_now", fake_live)
    monkeypatch.setattr(jobsmod, "sync_job", lambda jid: get_session().get(Job, jid))
    rv = client.get("/jobs/batch-g1/stream?interval=0.05")
    first = rv.data.decode().splitlines()[0]
    payload = json.loads(first.removeprefix("data: "))
    assert payload["minions"] == ["web01"]
    assert payload["live"] == 1


def test_job_sse_response_is_not_buffered(client):
    rv = client.get("/jobs/20260910123000000012/stream?interval=0.05")
    assert rv.mimetype == "text/event-stream"
    assert rv.headers["Cache-Control"] == "no-cache"
    assert rv.headers["X-Accel-Buffering"] == "no"
    # Consume the body: an unexhausted stream generator is closed by the
    # GC at an arbitrary later point and breaks other tests' teardown.
    assert "event: done" in rv.data.decode()


def test_events_sse_response_is_not_buffered(monkeypatch):
    from overstate_ui.salt_client import SaltClient

    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(SaltClient, "event_stream", lambda self: iter([]))
    app.extensions["salt_client"] = SaltClient("https://salt:8000", "u", "p")
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    rv = c.get("/events/stream?tag=salt/job")
    assert rv.mimetype == "text/event-stream"
    assert rv.headers["Cache-Control"] == "no-cache"
    assert rv.headers["X-Accel-Buffering"] == "no"
    # Consume the body: an unexhausted stream generator is closed by the
    # GC at an arbitrary later point and breaks other tests' teardown.
    assert "event: done" in rv.data.decode()


def test_events_stream_settles_done_vs_error():
    import pathlib

    text = (
        pathlib.Path(__file__).resolve().parent.parent
        / "overstate_ui"
        / "templates"
        / "events.html"
    ).read_text()
    assert "var finished = false" in text
    assert "if (finished) return;" in text
    assert "Stream ended (cap reached). Re-watch to resume." in text
    assert "Stream error or salt-api unreachable." in text

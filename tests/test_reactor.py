"""Reactor page: mapping list, SLS view, add/delete, export."""

import httpx

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import User
from overstate_ui.salt_client import SaltClient

PW = "test-password"

MAPPING = [
    {"salt/minion/*/start": ["salt://reactor/greet.sls"]},
    {"salt/auth": ["/srv/reactor/auth.sls"]},
]


NOT_RUNNING_BODY = """Exception occurred in runner reactor.list: Traceback (most recent call last):
  File "salt/client/mixins.py", line 387, in low
    data["return"] = func(*args, **kwargs)
  File "salt/runners/reactor.py", line 56, in list_
    raise CommandExecutionError("Reactor system is not running.")
salt.exceptions.CommandExecutionError: Reactor system is not running."""


def _transport(calls, fail=False, fail_body=None, ok_body=None):
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "runner":
            if fail:
                if fail_body is not None:
                    return httpx.Response(500, text=fail_body)
                return httpx.Response(500, json={"error": "down"})
            if ok_body is not None:
                return httpx.Response(200, text=ok_body)
            fun = body.get("fun")
            calls.append((fun, body))
            if fun == "reactor.list":
                return httpx.Response(200, json={"return": [MAPPING]})
            return httpx.Response(200, json={"return": [True]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def _app(tmp_path, calls=None, fail=False, fail_body=None, ok_body=None):
    calls = calls if calls is not None else []
    (tmp_path / "greet.sls").write_text("greet-new-minion:\n  test.nop: []\n")
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["REACTOR_ROOTS"] = str(tmp_path)
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000",
        "u",
        "p",
        transport=_transport(calls, fail, fail_body, ok_body),
    )
    with app.app_context():
        create_all()
        seed_admin(password=PW)
        session = get_session()
        session.add(
            User(username="op", password_hash=authmod._ph.hash(PW), role="operator")
        )
        session.add(
            User(username="vwr", password_hash=authmod._ph.hash(PW), role="viewer")
        )
        session.commit()
    return app


def _login(client, username):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": PW})


def test_index_lists_mapping(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "salt/minion/*/start" in html
    assert "salt://reactor/greet.sls" in html
    assert "/srv/reactor/auth.sls" in html
    assert "Export for git" in html
    assert "/events/" in html


def test_view_renders_sls(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    rv = client.get("/reactor/view", query_string={"sls": "greet.sls"})
    assert rv.status_code == 200
    assert "greet-new-minion" in rv.data.decode()


def test_view_traversal_404s(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    assert (
        client.get(
            "/reactor/view", query_string={"sls": "../salt/demo.sls"}
        ).status_code
        == 404
    )
    assert (
        client.get("/reactor/view", query_string={"sls": "/etc/passwd"}).status_code
        == 404
    )


def test_add_roundtrip(tmp_path):
    calls = []
    app = _app(tmp_path, calls)
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://reactor/key.sls"},
        follow_redirects=True,
    )
    assert "reactor added" in rv.data.decode()
    assert [c for c in calls if c[0] == "reactor.add"]


def test_add_rejects_bad_event(tmp_path):
    calls = []
    app = _app(tmp_path, calls)
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key; rm -rf /", "sls": "salt://reactor/key.sls"},
        follow_redirects=True,
    )
    assert "event pattern and one SLS" in rv.data.decode()
    assert not [c for c in calls if c[0] == "reactor.add"]


def test_delete_confirm_and_post(tmp_path):
    calls = []
    app = _app(tmp_path, calls)
    client = app.test_client()
    _login(client, "op")
    html = client.get(
        "/reactor/delete", query_string={"event": "salt/auth"}
    ).data.decode()
    assert "salt/auth" in html
    assert "Delete salt/auth" in html
    rv = client.post(
        "/reactor/delete", data={"event": "salt/auth"}, follow_redirects=True
    )
    assert "reactor deleted" in rv.data.decode()
    assert [c for c in calls if c[0] == "reactor.delete"]


def test_viewer_cannot_mutate(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "vwr")
    html = client.get("/reactor/").data.decode()
    assert "Add reactor" not in html
    assert (
        client.post("/reactor/add", data={"event": "x", "sls": "y"}).status_code == 403
    )
    assert client.post("/reactor/delete", data={"event": "x"}).status_code == 403


def test_export_yaml_and_download(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "vwr")
    html = client.get("/reactor/export").data.decode()
    assert "reactor:" in html
    assert "salt/minion/*/start" in html
    rv = client.get("/reactor/export", query_string={"download": "1"})
    assert rv.status_code == 200
    assert "attachment" in rv.headers.get("Content-Disposition", "")
    assert rv.data.decode().startswith("reactor:\n")


def test_list_failure_shows_error(tmp_path):
    app = _app(tmp_path, fail=True)
    client = app.test_client()
    _login(client, "op")
    rv = client.get("/reactor/")
    assert rv.status_code == 200
    assert "salt-api error" in rv.data.decode()


def test_not_running_master_shows_empty_state(tmp_path):
    app = _app(tmp_path, fail=True, fail_body=NOT_RUNNING_BODY)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "Reactor is not running on the master" in html
    assert "Reactor disabled" in html
    assert "Add reactor" not in html
    assert "Traceback" not in html
    export = client.get("/reactor/export").data.decode()
    assert "no mapping to export" in export


def test_traceback_in_200_payload_shows_empty_state(tmp_path):
    import json

    ok_body = json.dumps({"return": [NOT_RUNNING_BODY]})
    app = _app(tmp_path, ok_body=ok_body)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "Reactor is not running on the master" in html
    assert "Reactor disabled" in html
    assert "Traceback" not in html
    assert "Exception occurred in runner" not in html

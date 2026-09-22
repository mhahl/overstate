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
    assert "Event pattern and SLS reference required" in rv.data.decode()
    assert not [c for c in calls if c[0] == "reactor.add"]


def test_delete_flows_through_shared_dialog(tmp_path):
    calls = []
    app = _app(tmp_path, calls)
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "SLS file(s) stay on disk" in html
    assert client.get("/reactor/delete").status_code == 405
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
    assert "Reactor not configured" in html
    assert "Salt reactor docs" in html
    assert "Reactor disabled" in html
    # Wizard entry stays available when down: the review warns, and
    # admins can bootstrap via the master.conf persistence offer.
    assert "Add reactor" in html
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
    assert "Reactor not configured" in html
    assert "Reactor disabled" in html
    assert "Traceback" not in html
    assert "Exception occurred in runner" not in html


# -- Unit 5: reactor SLS bodies under the admin gate ------------------------

import hashlib

from overstate_ui.models import AuditEvent


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_edit_page_admin_only(tmp_path):
    app = _app(tmp_path)
    admin = app.test_client()
    _login(admin, "admin")
    rv = admin.get("/reactor/edit", query_string={"sls": "greet.sls"})
    assert rv.status_code == 200
    assert b"greet-new-minion" in rv.data
    for user in ("op", "vwr"):
        other = app.test_client()
        _login(other, user)
        assert (
            other.get("/reactor/edit", query_string={"sls": "greet.sls"}).status_code
            == 403
        )
        assert (
            other.post(
                "/reactor/save",
                data={"sls": "greet.sls", "content": "x: 1\n", "base_hash": "z"},
            ).status_code
            == 403
        )


def test_view_shows_edit_button_to_admins_only(tmp_path):
    app = _app(tmp_path)
    admin = app.test_client()
    _login(admin, "admin")
    assert (
        b"/reactor/edit"
        in admin.get("/reactor/view", query_string={"sls": "greet.sls"}).data
    )
    op = app.test_client()
    _login(op, "op")
    assert (
        b"/reactor/edit"
        not in op.get("/reactor/view", query_string={"sls": "greet.sls"}).data
    )


def test_save_writes_and_audits(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "admin")
    body = "greet-new-minion:\n  test.nop: []\n  grains.present: [os]\n"
    rv = client.post(
        "/reactor/save",
        data={
            "sls": "greet.sls",
            "content": body,
            "base_hash": _hash("greet-new-minion:\n  test.nop: []\n"),
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert (tmp_path / "greet.sls").read_text() == body
    with app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert "reactor-save:greet.sls" in actions


def test_save_stale_refuses(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "admin")
    stale = _hash("greet-new-minion:\n  test.nop: []\n")
    (tmp_path / "greet.sls").write_text("raced:\n  test.nop: []\n")
    rv = client.post(
        "/reactor/save",
        data={"sls": "greet.sls", "content": "mine: 1\n", "base_hash": stale},
        follow_redirects=True,
    )
    assert b"changed underneath you" in rv.data
    assert (tmp_path / "greet.sls").read_text() == "raced:\n  test.nop: []\n"
    with app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert "reactor-save-refused:greet.sls:stale" in actions


def test_save_invalid_yaml_blocked(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "admin")
    rv = client.post(
        "/reactor/save",
        data={
            "sls": "greet.sls",
            "content": "broken: [unclosed\n",
            "base_hash": _hash("greet-new-minion:\n  test.nop: []\n"),
        },
        follow_redirects=True,
    )
    assert b"Invalid YAML" in rv.data
    assert (tmp_path / "greet.sls").read_text() == (
        "greet-new-minion:\n  test.nop: []\n"
    )
    with app.app_context():
        actions = [row.action for row in get_session().query(AuditEvent).all()]
    assert "reactor-save-refused:greet.sls:invalid" in actions


def test_save_traversal_404s(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "admin")
    assert (
        client.post(
            "/reactor/save",
            data={"sls": "../salt/evil.sls", "content": "x", "base_hash": "z"},
        ).status_code
        == 404
    )
    assert not (tmp_path.parent / "evil.sls").exists()


def test_missing_roots_degrade_to_404(tmp_path):
    app = _app(tmp_path)
    app.config["REACTOR_ROOTS"] = str(tmp_path / "noroot")
    client = app.test_client()
    _login(client, "admin")
    assert (
        client.get("/reactor/view", query_string={"sls": "greet.sls"}).status_code
        == 404
    )
    assert (
        client.post(
            "/reactor/save",
            data={"sls": "greet.sls", "content": "x", "base_hash": "z"},
        ).status_code
        == 404
    )


# -- Trio: mapping fan-out across pods ---------------------------------------

from overstate_ui import reactor as reactormod


def _pod_transport(calls, mapping=None, fail=False):
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "runner":
            if fail:
                return httpx.Response(500, json={"error": "down"})
            fun = body.get("fun")
            calls.append((fun, body))
            if fun == "reactor.list":
                return httpx.Response(200, json={"return": [mapping or []]})
            return httpx.Response(200, json={"return": [True]})

    return httpx.MockTransport(handler)


def _pod_app(tmp_path, monkeypatch, specs):
    """App whose pod_clients fans out to one fake client per spec.

    Each spec is (calls, mapping, fail) for _pod_transport.
    """
    app = _app(tmp_path)
    pods = [
        (
            f"pod-{i}",
            SaltClient(
                f"https://pod-{i}:8000", "u", "p", transport=_pod_transport(*spec)
            ),
        )
        for i, spec in enumerate(specs)
    ]
    monkeypatch.setattr(reactormod, "pod_clients", lambda default: pods)
    return app, pods


def _actions(app):
    with app.app_context():
        return [row.action for row in get_session().query(AuditEvent).all()]


def test_add_fans_out_to_all_pods(tmp_path, monkeypatch):
    calls = [[], [], []]
    app, _ = _pod_app(
        tmp_path,
        monkeypatch,
        [(calls[0], [], False), (calls[1], [], False), (calls[2], [], False)],
    )
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://reactor/key.sls"},
        follow_redirects=True,
    )
    assert "reactor added." in rv.data.decode()
    for pod_calls in calls:
        assert [c for c in pod_calls if c[0] == "reactor.add"]
    assert "reactor-add:salt/key" in _actions(app)


def test_add_partial_when_pod_down(tmp_path, monkeypatch):
    calls = [[], []]
    app, _ = _pod_app(
        tmp_path, monkeypatch, [(calls[0], [], False), (calls[1], [], True)]
    )
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://reactor/key.sls"},
        follow_redirects=True,
    )
    html = rv.data.decode()
    assert "reactor added on 1 of 2 pod(s)" in html
    assert "unreachable" in html
    assert [c for c in calls[0] if c[0] == "reactor.add"]
    assert not [c for c in calls[1] if c[0] == "reactor.add"]
    assert "reactor-add:salt/key:partial" in _actions(app)


def test_add_refuses_when_no_pod_reachable(tmp_path, monkeypatch):
    app, _ = _pod_app(tmp_path, monkeypatch, [([], [], True)])
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://reactor/key.sls"},
        follow_redirects=True,
    )
    assert "no master reachable" in rv.data.decode()
    assert "reactor-add:salt/key" not in _actions(app)


def test_index_unions_pod_mappings(tmp_path, monkeypatch):
    app, _ = _pod_app(
        tmp_path,
        monkeypatch,
        [
            ([], [{"salt/minion/*/start": ["salt://reactor/greet.sls"]}], False),
            ([], [{"salt/auth": ["/srv/reactor/auth.sls"]}], False),
        ],
    )
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "salt/minion/*/start" in html
    assert "salt/auth" in html
    # Precedence banner names the persisted truth.
    assert "master.conf" in html
    assert "reactor:" in html


def test_index_flags_divergent_mapping(tmp_path, monkeypatch):
    app, _ = _pod_app(
        tmp_path,
        monkeypatch,
        [
            ([], [{"salt/key": ["salt://reactor/key.sls"]}], False),
            ([], [], False),
        ],
    )
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/").data.decode()
    assert "Only on some pods" in html
    assert "salt/key" in html


def test_delete_partial_when_pod_down(tmp_path, monkeypatch):
    calls = [[], []]
    app, _ = _pod_app(
        tmp_path, monkeypatch, [(calls[0], [], False), (calls[1], [], True)]
    )
    client = app.test_client()
    _login(client, "op")
    rv = client.post(
        "/reactor/delete", data={"event": "salt/auth"}, follow_redirects=True
    )
    html = rv.data.decode()
    assert "reactor deleted on 1 of 2 pod(s)" in html
    assert "reactor-delete:salt/auth:partial" in _actions(app)


# -- Wizard: stepped event -> SLS -> review -------------------------------


def _seed_audit(app, *actions):
    with app.app_context():
        session = get_session()
        for action in actions:
            session.add(AuditEvent(user="op", action=action))
        session.commit()


def test_wizard_step1_shows_presets_and_recents(tmp_path):
    app = _app(tmp_path)
    _seed_audit(app, "reactor-add:salt/custom-thing", "reactor-add:salt/auth")
    client = app.test_client()
    _login(client, "op")
    html = client.get("/reactor/add").data.decode()
    assert "Add reactor" in html
    assert "salt/minion/" in html  # preset from TAG_CHOICES
    assert "salt/custom-thing" in html  # recent, non-preset
    assert html.count("salt/auth") >= 1  # preset still listed once


def test_wizard_viewer_forbidden(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "vwr")
    assert client.get("/reactor/add").status_code == 403
    assert client.post("/reactor/add/step2", data={"event": "x"}).status_code == 403
    assert (
        client.post("/reactor/add/review", data={"event": "x", "sls": "y"}).status_code
        == 403
    )


def test_wizard_step2_rejects_bad_event(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    rv = client.post("/reactor/add/step2", data={"event": "bad; rm -rf"})
    html = rv.data.decode()
    assert "no spaces" in html
    assert "bad; rm -rf" in html  # input preserved


def test_wizard_step2_lists_files(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    html = client.post("/reactor/add/step2", data={"event": "salt/key"}).data.decode()
    assert "salt://greet.sls" in html
    assert "SLS file" in html


def test_wizard_review_shows_blast_radius(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    html = client.post(
        "/reactor/add/review",
        data={"event": "salt/key", "sls": "salt://greet.sls"},
    ).data.decode()
    assert "Fan-out" in html
    assert "master privileges" in html
    assert "Confirm and add" in html
    assert "Also record" not in html  # operator sees no persist offer


def test_wizard_review_admin_gets_persist_offer(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "admin")
    html = client.post(
        "/reactor/add/review",
        data={"event": "salt/key", "sls": "salt://greet.sls"},
    ).data.decode()
    assert "Also record" in html
    assert "master.conf" in html


def test_wizard_review_rejects_bad_sls(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    rv = client.post("/reactor/add/review", data={"event": "salt/key", "sls": ";;;"})
    html = rv.data.decode()
    assert "Pick an SLS file" in html
    assert "salt/key" in html  # event carried back


def test_wizard_review_warns_when_reactor_down(tmp_path):
    app = _app(tmp_path, fail=True, fail_body=NOT_RUNNING_BODY)
    client = app.test_client()
    _login(client, "op")
    html = client.post(
        "/reactor/add/review",
        data={"event": "salt/key", "sls": "salt://greet.sls"},
    ).data.decode()
    assert "is not running" in html
    assert "Traceback" not in html


def test_wizard_review_custom_unbrowsable_warns(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _login(client, "op")
    html = client.post(
        "/reactor/add/review",
        data={"event": "salt/key", "sls": "/elsewhere/x.sls"},
    ).data.decode()
    assert "outside the reactor roots" in html


# -- Wizard persistence: stanza merge -------------------------------------


def test_build_persisted_text_empty_block():
    from overstate_ui.reactor import build_persisted_text

    new, verdict = build_persisted_text(
        "# masters\nreactor: []\n", "salt/key", "salt://reactor/key.sls"
    )
    assert verdict == "added"
    assert "# masters" in new
    assert "salt/key" in new
    assert "reactor: []" not in new


def test_build_persisted_text_populated_block_inserts():
    from overstate_ui.reactor import build_persisted_text

    current = (
        "# c\nreactor:\n  - 'salt/auth':\n    - salt://reactor/auth.sls\nother: 1\n"
    )
    new, verdict = build_persisted_text(current, "salt/key", "salt://r/key.sls")
    assert verdict == "added"
    assert "other: 1" in new
    assert "salt/auth" in new and "salt/key" in new


def test_build_persisted_text_present_and_invalid():
    from overstate_ui.reactor import build_persisted_text

    current = "reactor:\n  - 'salt/key':\n    - salt://r/key.sls\n"
    _, verdict = build_persisted_text(current, "salt/key", "salt://r/key.sls")
    assert verdict == "present"
    _, verdict = build_persisted_text("reactor: [unclosed\n", "x", "y")
    assert verdict == "invalid"


def test_confirm_with_persist_offline_still_adds(tmp_path):
    calls = []
    app = _app(tmp_path, calls)
    client = app.test_client()
    _login(client, "admin")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://r/key.sls", "persist": "1"},
        follow_redirects=True,
    )
    html = rv.data.decode()
    assert "reactor added." in html  # runner path unaffected
    assert "No cluster connection" in html  # persist refused offline
    actions = _actions(app)
    assert "reactor-add:salt/key" in actions
    assert "masterconfig-save-refused:master.conf:persist-offline" in actions


def test_confirm_with_persist_records_stanza(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from overstate_ui import masterconfig

    calls = []
    app = _app(tmp_path, calls)
    fake = SimpleNamespace(
        data={"master.conf": "# c\nreactor: []\n"},
        rv="11",
        replaced=None,
        config=SimpleNamespace(namespace="overstate"),
    )

    def get_configmap(name):
        assert name == "salt-master-config"
        return {"data": dict(fake.data), "resourceVersion": fake.rv}

    def replace_configmap(name, data, base_rv):
        assert base_rv == "11"
        fake.replaced = data
        return "12"

    snapshots = []
    monkeypatch.setattr(
        reactormod,
        "K8sClient",
        lambda: SimpleNamespace(
            config=fake.config,
            get_configmap=get_configmap,
            replace_configmap=replace_configmap,
        ),
    )
    monkeypatch.setattr(
        masterconfig,
        "_snapshot",
        lambda *a: snapshots.append(a),
    )
    client = app.test_client()
    _login(client, "admin")
    rv = client.post(
        "/reactor/add",
        data={"event": "salt/key", "sls": "salt://r/key.sls", "persist": "1"},
        follow_redirects=True,
    )
    assert "Recorded in the master.conf stanza" in rv.data.decode()
    assert len(snapshots) == 1  # snapshot-first
    assert "salt/key" in fake.replaced["master.conf"]
    assert "# c" in fake.replaced["master.conf"]
    assert "masterconfig-save:master.conf:12" in _actions(app)

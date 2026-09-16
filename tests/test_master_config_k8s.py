"""Master-config k8s tests, Unit 1: stdlib in-cluster client.

Fake transport stands in for the API server; no network is touched.
Later units (editor, restart route, accept-on-both) extend this file.
"""

import json

import pytest

from overstate_ui.k8s import (
    RESTART_ANNOTATION,
    K8sClient,
    K8sConfig,
    K8sConflictError,
    K8sError,
    K8sUnavailableError,
)


def _client(calls, handler):
    config = K8sConfig(
        server="https://10.0.0.1:443",
        token="test-token-value",
        namespace="overstate",
        ca_path=None,
    )

    def transport(method, url, body, token, ca_path, timeout):
        calls.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body.decode()) if body else None,
                "token": token,
            }
        )
        return handler(method, url, calls[-1]["body"])

    return K8sClient(config=config, transport=transport)


def test_get_configmap_shape():
    calls = []
    client = _client(
        calls,
        lambda m, u, b: (
            200,
            {"data": {"master.conf": "# hi\n"}, "metadata": {"resourceVersion": "11"}},
        ),
    )
    assert client.get_configmap("salt-master-config") == {
        "data": {"master.conf": "# hi\n"},
        "resourceVersion": "11",
    }
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith(
        "/api/v1/namespaces/overstate/configmaps/salt-master-config"
    )


def test_replace_carries_base_resource_version():
    calls = []
    client = _client(
        calls, lambda m, u, b: (200, {"metadata": {"resourceVersion": "12"}})
    )
    assert client.replace_configmap("salt-master-config", {"a": "b"}, "11") == "12"
    assert calls[0]["method"] == "PUT"
    assert calls[0]["body"]["metadata"]["resourceVersion"] == "11"
    assert calls[0]["body"]["data"] == {"a": "b"}


def test_stale_base_refuses_with_conflict():
    calls = []
    client = _client(
        calls,
        lambda m, u, b: (_ for _ in ()).throw(
            K8sConflictError("object changed since read: conflict")
        ),
    )
    with pytest.raises(K8sConflictError):
        client.replace_configmap("salt-master-config", {"a": "b"}, "9")
    assert len(calls) == 1  # single PUT attempt; server refused it


def test_api_error_becomes_k8s_error():
    client = _client([], lambda m, u, b: (500, {}))
    with pytest.raises(K8sError):
        client.get_configmap("salt-master-config")


def test_restart_patch_stamps_annotation():
    calls = []
    client = _client(calls, lambda m, u, b: (200, {}))
    client.restart_statefulset("salt-master")
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith(
        "/apis/apps/v1/namespaces/overstate/statefulsets/salt-master"
    )
    annotations = calls[0]["body"]["spec"]["template"]["metadata"]["annotations"]
    assert RESTART_ANNOTATION in annotations
    assert annotations[RESTART_ANNOTATION].endswith("Z")


def test_refusal_outside_cluster_never_calls_api():
    calls = []
    client = K8sClient(
        config=K8sConfig(server=None, token=None, namespace="overstate", ca_path=None),
        transport=lambda *a: calls.append(a) or (200, {}),
    )
    with pytest.raises(K8sUnavailableError) as exc:
        client.replace_configmap("salt-master-config", {}, "")
    assert "kubectl" in str(exc.value)
    assert calls == []


def test_token_travels_in_header_only():
    calls = []
    client = _client(calls, lambda m, u, b: (200, {"metadata": {}}))
    client.replace_configmap("salt-master-config", {"a": "b"}, "11")
    call = calls[0]
    assert call["token"] == "test-token-value"
    assert "test-token-value" not in call["url"]
    assert "test-token-value" not in json.dumps(call["body"])


def test_statefulset_rollout_snapshot():
    client = _client(
        [],
        lambda m, u, b: (
            200,
            {
                "metadata": {"generation": 4},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 4,
                    "readyReplicas": 2,
                    "updatedReplicas": 2,
                },
            },
        ),
    )
    assert client.statefulset_rollout("salt-master") == {
        "observedGeneration": 4,
        "generation": 4,
        "replicas": 2,
        "readyReplicas": 2,
        "updatedReplicas": 2,
    }


def test_pods_ready_counts():
    def pod(ready):
        return {"status": {"conditions": [{"type": "Ready", "status": ready}]}}

    client = _client([], lambda m, u, b: (200, {"items": [pod("True"), pod("False")]}))
    assert client.pods_ready("app.kubernetes.io/name=salt-master") == (1, 2)


# -- Unit 3: Master Config browser/editor ---------------------------------

import overstate_ui.masterconfig as masterconfig_mod
from overstate_ui import create_app
from overstate_ui.auth import _ph, seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent, User


class FakeK8s:
    """In-memory API server: live + history ConfigMaps with real RVs."""

    def __init__(self):
        self.config = K8sConfig(
            server="https://10.0.0.1:443",
            token="t",
            namespace="overstate",
            ca_path=None,
        )
        self.live = {
            "data": {
                "master.conf": "# base\n",
                "api.conf": "external_auth: {}\n",
            },
            "rv": "10",
        }
        self.history = {"data": {}, "rv": "5"}
        self.calls = []
        self.unavailable = False
        self.sts = {
            "generation": 3,
            "observed": 3,
            "replicas": 1,
            "ready": 1,
            "updated": 1,
        }
        self.converge = True
        self.stamps = []

    def _store(self, name):
        assert name in ("salt-master-config", "salt-master-config-history")
        return self.live if name == "salt-master-config" else self.history

    def get_configmap(self, name):
        self.calls.append(("get", name))
        if self.unavailable:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        store = self._store(name)
        return {"data": dict(store["data"]), "resourceVersion": store["rv"]}

    def replace_configmap(self, name, data, base_rv):
        self.calls.append(("replace", name))
        if self.unavailable:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        store = self._store(name)
        if base_rv != store["rv"]:
            raise K8sConflictError("object changed since read")
        store["data"] = dict(data)
        store["rv"] = str(int(store["rv"]) + 1)
        return store["rv"]

    def restart_statefulset(self, name):
        assert name == "salt-master"
        self.calls.append(("restart", name))
        if self.unavailable:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        self.stamps.append(name)
        self.sts["generation"] += 1
        if self.converge:
            self.sts["observed"] = self.sts["generation"]

    def statefulset_rollout(self, name):
        assert name == "salt-master"
        self.calls.append(("rollout", name))
        if self.unavailable:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        return {
            "observedGeneration": self.sts["observed"],
            "generation": self.sts["generation"],
            "replicas": self.sts["replicas"],
            "readyReplicas": self.sts["ready"],
            "updatedReplicas": self.sts["updated"],
        }


@pytest.fixture()
def mui(tmp_path, monkeypatch):
    fake = FakeK8s()
    monkeypatch.setattr(masterconfig_mod, "K8sClient", lambda: fake)
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(tmp_path)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(User(username="op", role="operator", password_hash=_ph.hash("pw")))
        session.add(User(username="v", role="viewer", password_hash=_ph.hash("pw")))
        session.commit()

    def login(username):
        client = app.test_client()
        client.post("/login", data={"username": username, "password": "pw"})
        client.app = app
        return client

    return {"app": app, "fake": fake, "login": login}


def _actions(client):
    with client.app.app_context():
        session = get_session()
        try:
            return [row.action for row in session.query(AuditEvent).all()]
        finally:
            session.close()


def test_admin_lists_keys(mui):
    rv = mui["login"]("admin").get("/settings/master/")
    assert rv.status_code == 200
    assert b"master.conf" in rv.data and b"api.conf" in rv.data


def test_legacy_prefix_forwards(mui):
    client = mui["login"]("admin")
    rv = client.get("/master-config/view", query_string={"key": "api.conf"})
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/settings/master/view?key=api.conf")


def test_settings_tabs_switch_sides(mui):
    admin = mui["login"]("admin")
    server = admin.get("/settings/").data.decode()
    assert "Server Settings" in server and "Master Settings" in server
    master = admin.get("/settings/master/").data.decode()
    assert "Master Settings" in master
    op = mui["login"]("op")
    ope = op.get("/settings/").data.decode()
    assert "Server Settings" in ope and "Master Settings" not in ope


def test_operator_and_viewer_forbidden(mui):
    for user in ("op", "v"):
        client = mui["login"](user)
        assert client.get("/settings/master/").status_code == 403
        assert (
            client.post(
                "/settings/master/save",
                data={"key": "master.conf", "content": "x: 1\n"},
            ).status_code
            == 403
        )


def test_unknown_key_404s(mui):
    client = mui["login"]("admin")
    assert (
        client.get("/settings/master/view", query_string={"key": "nope"}).status_code
        == 404
    )
    assert (
        client.get("/settings/master/edit", query_string={"key": "nope"}).status_code
        == 404
    )
    assert (
        client.post(
            "/settings/master/save", data={"key": "nope", "content": "x"}
        ).status_code
        == 404
    )


def test_valid_save_snapshots_history_before_live_write(mui):
    fake = mui["fake"]
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "# changed\n",
            "base_resource_version": "10",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert b"# changed" in rv.data
    assert fake.live["data"]["master.conf"] == "# changed\n"
    import json as _json

    revisions = _json.loads(fake.history["data"]["history.json"])
    assert len(revisions) == 1
    assert revisions[0]["data"]["master.conf"] == "# base\n"
    assert revisions[0]["user"] == "admin"
    replaces = [c for c in fake.calls if c[0] == "replace"]
    assert replaces[0][1] == "salt-master-config-history"
    assert replaces[1][1] == "salt-master-config"
    assert any(a.startswith("masterconfig-save:master.conf:") for a in _actions(client))


def test_stale_save_refuses_and_writes_nothing(mui):
    fake = mui["fake"]
    fake.live["rv"] = "11"  # raced external edit after the form was read
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "# raced\n",
            "base_resource_version": "10",
        },
        follow_redirects=True,
    )
    assert b"changed underneath you" in rv.data
    assert fake.live["data"]["master.conf"] == "# base\n"
    assert any(
        a == "masterconfig-save-refused:master.conf:stale" for a in _actions(client)
    )


def test_invalid_yaml_blocked_with_live_untouched(mui):
    fake = mui["fake"]
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "foo: [unclosed\n",
            "base_resource_version": "10",
        },
    )
    assert rv.status_code == 200
    assert b"Invalid YAML" in rv.data
    assert fake.live["data"]["master.conf"] == "# base\n"
    assert any(
        a == "masterconfig-save-refused:master.conf:invalid" for a in _actions(client)
    )


def test_non_mapping_yaml_blocked(mui):
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "- just\n- a\n- list\n",
            "base_resource_version": "10",
        },
    )
    assert b"top-level mapping" in rv.data
    assert mui["fake"].live["data"]["master.conf"] == "# base\n"


def test_empty_config_saves_fine(mui):
    fake = mui["fake"]
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "",
            "base_resource_version": "10",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert fake.live["data"]["master.conf"] == ""


def test_identical_content_is_noop(mui):
    fake = mui["fake"]
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "# base\n",
            "base_resource_version": "10",
        },
        follow_redirects=True,
    )
    assert b"No changes" in rv.data
    assert fake.history["data"] == {}
    assert not [a for a in _actions(client) if a.startswith("masterconfig-save")]


def test_offline_degrades_with_kubectl_hint(mui):
    mui["fake"].unavailable = True
    client = mui["login"]("admin")
    rv = client.get("/settings/master/")
    assert rv.status_code == 200
    assert b"kubectl -n overstate edit configmap salt-master-config" in rv.data
    rv = client.post(
        "/settings/master/save",
        data={"key": "master.conf", "content": "x: 1\n"},
        follow_redirects=True,
    )
    assert b"kubectl -n overstate edit configmap" in rv.data
    assert any(
        a == "masterconfig-save-refused:master.conf:offline" for a in _actions(client)
    )


def test_auth_banner_on_api_conf_only(mui):
    client = mui["login"]("admin")
    edit_api = client.get("/settings/master/edit", query_string={"key": "api.conf"})
    assert b"Lockout risk" in edit_api.data
    edit_master = client.get(
        "/settings/master/edit", query_string={"key": "master.conf"}
    )
    assert b"Lockout risk" not in edit_master.data


def test_nav_link_admin_only(mui):
    admin_page = mui["login"]("admin").get("/files/").data
    assert b"Master config" in admin_page
    viewer_page = mui["login"]("v").get("/files/").data
    assert b"Master config" not in viewer_page


# -- Unit 4: restart + revert ------------------------------------------------


@pytest.fixture()
def m4(mui, monkeypatch):
    monkeypatch.setattr(masterconfig_mod, "RESTART_MAX_POLLS", 4)
    monkeypatch.setattr(masterconfig_mod, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(masterconfig_mod, "_salt_api_healthy", lambda: True)
    return mui


def _save(client, content="# changed\n", base="10"):
    return client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": content,
            "base_resource_version": base,
        },
        follow_redirects=True,
    )


def test_operator_restart_and_revert_forbidden(m4):
    client = m4["login"]("op")
    assert client.post("/settings/master/restart").status_code == 403
    assert client.post("/settings/master/revert").status_code == 403


def test_restart_offline_names_manual_command(m4):
    m4["fake"].unavailable = True
    m4["fake"].config = K8sConfig(
        server=None, token=None, namespace="overstate", ca_path=None
    )
    client = m4["login"]("admin")
    rv = client.post("/settings/master/restart", follow_redirects=True)
    assert b"rollout restart statefulset/salt-master" in rv.data
    assert m4["fake"].stamps == []
    assert any(a == "master-restart:refused-offline" for a in _actions(client))


def test_restart_success(m4):
    client = m4["login"]("admin")
    rv = client.post("/settings/master/restart", follow_redirects=True)
    assert b"Masters restarted and healthy" in rv.data
    assert m4["fake"].stamps == ["salt-master"]
    assert any(a == "master-restart:ok" for a in _actions(client))


def test_restart_timeout_links_revert(m4):
    m4["fake"].converge = False
    client = m4["login"]("admin")
    rv = client.post("/settings/master/restart", follow_redirects=True)
    assert b"did not finish in time" in rv.data
    assert b"revert to the last snapshot" in rv.data
    assert any(a == "master-restart:timeout" for a in _actions(client))


def test_restart_api_down_is_unhealthy(m4, monkeypatch):
    monkeypatch.setattr(masterconfig_mod, "_salt_api_healthy", lambda: False)
    client = m4["login"]("admin")
    rv = client.post("/settings/master/restart", follow_redirects=True)
    assert b"salt-api did not come back healthy" in rv.data
    assert any(a == "master-restart:timeout" for a in _actions(client))


def test_revert_empty_history_writes_nothing(m4):
    fake = m4["fake"]
    client = m4["login"]("admin")
    rv = client.post("/settings/master/revert", follow_redirects=True)
    assert b"No snapshots yet" in rv.data
    assert fake.live["data"]["master.conf"] == "# base\n"
    assert fake.stamps == []
    assert any(a == "masterconfig-revert:refused-empty" for a in _actions(client))


def test_revert_last_restores_snapshot_and_restarts(m4):
    import json as _json

    fake = m4["fake"]
    client = m4["login"]("admin")
    _save(client)
    assert fake.live["data"]["master.conf"] == "# changed\n"
    rv = client.post("/settings/master/revert", follow_redirects=True)
    assert b"Reverted and restarted healthy" in rv.data
    assert fake.live["data"]["master.conf"] == "# base\n"
    assert fake.stamps == ["salt-master"]
    revisions = _json.loads(fake.history["data"]["history.json"])
    assert revisions[-1]["data"]["master.conf"] == "# changed\n"
    assert any(
        a.startswith("masterconfig-revert:") and not a.endswith(":timeout")
        for a in _actions(client)
    )


def test_oversize_save_refuses(mui):
    from overstate_ui.files import MAX_BYTES

    fake = mui["fake"]
    client = mui["login"]("admin")
    rv = client.post(
        "/settings/master/save",
        data={
            "key": "master.conf",
            "content": "x" * (MAX_BYTES + 1),
            "base_resource_version": "10",
        },
        follow_redirects=True,
    )
    assert b"Too large to save" in rv.data
    assert fake.live["data"]["master.conf"] == "# base\n"
    assert any(
        a == "masterconfig-save-refused:master.conf:oversize" for a in _actions(client)
    )

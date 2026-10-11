"""Tests for on-demand salt-cli pod spawning, RBAC, execution, and cleanup."""

import datetime
import json
import pytest
from unittest.mock import MagicMock

from overstate_ui import auth as authmod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.console import get_or_spawn_pod
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.k8s import K8sClient, K8sConfig, K8sError
from overstate_ui.models import User
from overstate_ui.salt_client import SaltClient


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.pods = {}

    def __call__(self, method, url, body, token, ca_path, timeout):
        self.calls.append((method, url, body))
        path = url.split("://")[1].split("/", 1)[1] if "://" in url else url

        # POST /api/v1/namespaces/overstate/pods
        if method == "POST" and "/pods" in path:
            data = json.loads(body.decode("utf-8")) if body else {}
            name = data.get("metadata", {}).get("name", "pod-1")
            data["status"] = {"phase": "Running"}
            self.pods[name] = data
            return 201, data

        # GET /api/v1/namespaces/overstate/pods/<name>
        if method == "GET" and "/pods/" in path and "?" not in path.split("/pods/")[1]:
            name = path.split("/pods/")[1]
            if name in self.pods:
                return 200, self.pods[name]
            return 404, {"message": f"pods \"{name}\" not found"}

        # GET /api/v1/namespaces/overstate/pods?labelSelector=...
        if method == "GET" and "/pods" in path:
            items = []
            selector = ""
            if "labelSelector=" in path:
                selector = path.split("labelSelector=")[1].split("&")[0]
            req_labels = {}
            if selector:
                for pair in selector.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        req_labels[k] = v

            for pod in self.pods.values():
                labels = pod.get("metadata", {}).get("labels", {})
                match = True
                for k, v in req_labels.items():
                    if labels.get(k) != v:
                        match = False
                        break
                if match:
                    items.append(pod)
            return 200, {"items": items}

        # DELETE /api/v1/namespaces/overstate/pods/<name>
        if method == "DELETE" and "/pods/" in path:
            name = path.split("/pods/")[1].split("?")[0]
            if name in self.pods:
                deleted = self.pods.pop(name)
                return 200, deleted
            return 404, {"message": f"pods \"{name}\" not found"}

        return 200, {}


def test_k8s_client_pod_lifecycle():
    transport = FakeTransport()
    config = K8sConfig("https://10.43.0.1:443", "test-token", "overstate", None)
    client = K8sClient(config, transport=transport)

    manifest = {
        "metadata": {"name": "salt-cli-admin-1234"},
        "spec": {
            "serviceAccountName": "salt-cli-admin",
            "activeDeadlineSeconds": 1800,
            "containers": [{"name": "salt-cli", "image": "quay.io/sigaint/overstate:salt-cli"}],
        },
    }

    created = client.create_pod(manifest)
    assert created["metadata"]["name"] == "salt-cli-admin-1234"
    assert created["metadata"]["namespace"] == "overstate"

    fetched = client.get_pod("salt-cli-admin-1234")
    assert fetched is not None
    assert fetched["spec"]["serviceAccountName"] == "salt-cli-admin"

    deleted = client.delete_pod("salt-cli-admin-1234")
    assert deleted is not None
    assert client.get_pod("salt-cli-admin-1234") is None


def test_k8s_client_cleanup_salt_cli_pods():
    transport = FakeTransport()
    config = K8sConfig("https://10.43.0.1:443", "test-token", "overstate", None)
    client = K8sClient(config, transport=transport)

    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    transport.pods["pod-deadlined"] = {
        "metadata": {"name": "pod-deadlined", "labels": {"app.kubernetes.io/name": "salt-cli"}},
        "status": {"phase": "Failed", "reason": "DeadlineExceeded"},
    }
    transport.pods["pod-completed"] = {
        "metadata": {"name": "pod-completed", "labels": {"app.kubernetes.io/name": "salt-cli"}},
        "status": {"phase": "Succeeded"},
    }
    transport.pods["pod-running"] = {
        "metadata": {"name": "pod-running", "labels": {"app.kubernetes.io/name": "salt-cli"}, "creationTimestamp": now_iso},
        "status": {"phase": "Running"},
    }

    cleaned = client.cleanup_salt_cli_pods(max_age_seconds=600)
    assert "pod-deadlined" in cleaned
    assert "pod-completed" in cleaned
    assert "pod-running" in transport.pods


def test_get_or_spawn_pod_role_assignment():
    transport = FakeTransport()
    config = K8sConfig("https://10.43.0.1:443", "test-token", "overstate", None)
    client = K8sClient(config, transport=transport)

    # Test viewer user
    viewer = User(username="alice", role="viewer")
    pod_name, ready = get_or_spawn_pod(viewer, client)
    assert pod_name.startswith("salt-cli-alice-")
    assert ready is True
    pod_obj = client.get_pod(pod_name)
    assert pod_obj["spec"]["serviceAccountName"] == "salt-cli-viewer"
    env_names = [e["name"] for e in pod_obj["spec"]["containers"][0]["env"]]
    assert "OVERSTATE_ROLE" in env_names
    assert "SALT_API_USER_PASS" not in env_names  # Viewer does NOT get salt-api password

    # Test operator user
    operator = User(username="bob", role="operator")
    pod_name_op, ready_op = get_or_spawn_pod(operator, client)
    assert pod_name_op.startswith("salt-cli-bob-")
    pod_obj_op = client.get_pod(pod_name_op)
    assert pod_obj_op["spec"]["serviceAccountName"] == "salt-cli-operator"
    env_names_op = [e["name"] for e in pod_obj_op["spec"]["containers"][0]["env"]]
    assert "SALT_API_USER_PASS" in env_names_op

    # Test admin user
    admin = User(username="carol", role="admin")
    pod_name_adm, ready_adm = get_or_spawn_pod(admin, client)
    assert pod_name_adm.startswith("salt-cli-carol-")
    pod_obj_adm = client.get_pod(pod_name_adm)
    assert pod_obj_adm["spec"]["serviceAccountName"] == "salt-cli-admin"


def test_console_cluster_run_and_exit(monkeypatch):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False

    transport = FakeTransport()
    config = K8sConfig("https://10.43.0.1:443", "test-token", "overstate", None)
    fake_k8s = K8sClient(config, transport=transport)

    def fake_exec(pod_name, command, container=None, timeout=30.0):
        cmd_str = command[2] if len(command) > 2 else ""
        if "test.ping" in cmd_str:
            return 0, "minion1: True\n", ""
        if "whoami" in cmd_str:
            return 0, "salt\n", ""
        if "kubectl get pods" in cmd_str:
            return 0, "overstate-app-xxx Running\n", ""
        return 0, f"executed: {cmd_str}\n", ""

    fake_k8s._exec_transport = fake_exec
    monkeypatch.setattr("overstate_ui.console.K8sClient", lambda: fake_k8s)

    with app.app_context():
        create_all()
        seed_admin(password="pw")
        get_session().add(User(username="op", password_hash=authmod._ph.hash("pw"), role="operator"))
        get_session().commit()

    c = app.test_client()
    c.post("/login", data={"username": "op", "password": "pw"})

    # Check status endpoint
    res = c.get("/console/pod-status")
    assert res.status_code == 200
    assert res.get_json()["available"] is True

    # Run command -> should spawn pod and return command output
    res = c.post("/console/run", json={"line": "salt '*' test.ping"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "minion1: True" in data["output"]
    assert "salt-cli-op-" in data["pod"]

    # Run kubectl command inside pod
    res = c.post("/console/run", json={"line": "kubectl get pods"})
    assert res.status_code == 200
    assert "overstate-app-xxx" in res.get_json()["output"]

    # Exit -> should destroy pod immediately
    res = c.post("/console/run", json={"line": "exit"})
    assert res.status_code == 200
    assert res.get_json()["closed"] is True
    assert "Session closed. Pod destroyed." in res.get_json()["output"]

    # Destroy endpoint
    res = c.post("/console/destroy")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

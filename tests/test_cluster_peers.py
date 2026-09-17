"""cluster-peers.py tests: API member discovery for the Salt master entrypoint.

Pure parsing is unit-tested by import; transport goes through a stub
HTTP API server so no cluster is needed. Any failure mode must exit
non-zero with empty stdout: the entrypoint treats that as "fall back
to DNS", so a failed probe must never print a partial member set.
"""

import http.server
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PEERS_PY = REPO / "cluster-peers.py"


def _load():
    spec = importlib.util.spec_from_file_location("cluster_peers", PEERS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ep(pod, ip="10.0.0.9"):
    return {"addresses": [ip], "targetRef": {"kind": "Pod", "name": pod}}


def test_slice_names_maps_targetref_to_dns():
    mod = _load()
    doc = {"endpoints": [_ep("salt-master-0"), _ep("salt-master-1")]}
    assert mod.slice_names(doc, "salt-master") == [
        "salt-master-0.salt-master",
        "salt-master-1.salt-master",
    ]


def test_slice_names_skips_endpoints_without_targetref():
    mod = _load()
    doc = {
        "endpoints": [
            {"addresses": ["10.42.3.161"]},
            {"addresses": ["10.42.3.162"], "targetRef": {}},
            {"addresses": ["10.42.3.163"], "targetRef": {"name": None}},
            _ep("salt-master-2", "10.42.3.164"),
        ]
    }
    assert mod.slice_names(doc, "salt-master") == ["salt-master-2.salt-master"]


def test_slice_names_tolerates_missing_keys():
    mod = _load()
    assert mod.slice_names({}, "salt-master") == []
    assert mod.slice_names({"endpoints": None}, "salt-master") == []


def test_member_names_dedupes_sorts_and_rejects_empty():
    mod = _load()
    doc = {
        "items": [
            {"endpoints": [_ep("salt-master-1"), _ep("salt-master-0")]},
            {"endpoints": [_ep("salt-master-0"), _ep("salt-master-2")]},
        ]
    }
    assert mod.member_names(doc, "salt-master") == [
        "salt-master-0.salt-master",
        "salt-master-1.salt-master",
        "salt-master-2.salt-master",
    ]
    with pytest.raises(mod.ApiError):
        mod.member_names({"items": []}, "salt-master")


def test_unknown_mode_fails_without_network():
    mod = _load()
    with pytest.raises(mod.ApiError):
        mod.main(["cluster-peers.py", "bogus"], {})


class _StubServer:
    """Minimal HTTP API stub; records request paths for assertions."""

    def __init__(self, payload, status=200):
        self.paths = []
        body = json.dumps(payload).encode()
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parent.paths.append(self.path)
                out = body if status == 200 else b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.thread.join()


def _api_env(tmp_path, stub, token="test-token"):
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n", encoding="utf-8")
    return {
        **os.environ,
        "K8S_API_SCHEME": "http",
        "K8S_API_HOST": "127.0.0.1",
        "K8S_API_PORT": str(stub.port),
        "K8S_NAMESPACE": "overstate",
        "SA_TOKEN_FILE": str(token_file),
    }


def _run(args, env):
    return subprocess.run(
        [sys.executable, str(PEERS_PY), *args],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_peers_mode_roundtrip_lists_sorted_unique_names(tmp_path):
    payload = {
        "items": [
            {
                "endpoints": [
                    _ep("salt-master-1", "10.42.1.246"),
                    _ep("salt-master-0", "10.42.3.161"),
                ]
            },
            {
                "endpoints": [
                    _ep("salt-master-0", "10.42.3.161"),
                    _ep("salt-master-2", "10.42.0.40"),
                ]
            },
        ]
    }
    stub = _StubServer(payload)
    try:
        proc = _run([], _api_env(tmp_path, stub))
    finally:
        stub.close()
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == [
        "salt-master-0.salt-master",
        "salt-master-1.salt-master",
        "salt-master-2.salt-master",
    ]
    assert any("labelSelector" in path for path in stub.paths)


def test_peers_mode_empty_set_fails_silently(tmp_path):
    stub = _StubServer({"items": []})
    try:
        proc = _run([], _api_env(tmp_path, stub))
    finally:
        stub.close()
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_peers_mode_api_error_fails_silently(tmp_path):
    stub = _StubServer({}, status=403)
    try:
        proc = _run([], _api_env(tmp_path, stub))
    finally:
        stub.close()
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_replicas_mode_prints_statefulset_count(tmp_path):
    stub = _StubServer({"spec": {"replicas": 3}})
    try:
        proc = _run(["replicas"], _api_env(tmp_path, stub))
    finally:
        stub.close()
    assert proc.returncode == 0
    assert proc.stdout.strip() == "3"
    assert any("statefulsets/salt-master" in path for path in stub.paths)


def test_missing_token_fails_silently(tmp_path):
    stub = _StubServer({"items": []})
    try:
        env = _api_env(tmp_path, stub)
        env["SA_TOKEN_FILE"] = str(tmp_path / "no-such-token")
        proc = _run([], env)
    finally:
        stub.close()
    assert proc.returncode != 0
    assert proc.stdout == ""

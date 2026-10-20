"""Kubernetes exec: v4 channel parsing, exit codes, and failure modes."""

import json

import pytest

from overstate_ui.k8s import K8sClient, K8sConfig, K8sError, K8sUnavailableError


def _status(**payload) -> bytes:
    body = {"metadata": {}, "status": "Success"}
    body.update(payload)
    return b"\x03" + json.dumps(body).encode()


class FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def recv(self):
        if not self.frames:
            raise ConnectionError("closed")
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def _client(frames, calls=None):
    calls = calls if calls is not None else []

    def factory(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeWS(frames)

    config = K8sConfig(
        server="https://10.0.0.1:443",
        token="test-token",
        namespace="overstate",
        ca_path="/var/run/secrets/ca.crt",
    )
    return K8sClient(config=config, ws_factory=factory), calls


def test_exec_collects_streams_and_exit_code():
    calls = []
    client, calls = _client(
        [b"\x01hello ", b"\x02warn", b"\x01world", _status()], calls
    )
    out = client.exec_command("salt-master-0", ["/bin/echo", "hi there"])
    assert out == {
        "ok": True,
        "exit_code": 0,
        "stdout": "hello world",
        "stderr": "warn",
    }
    call = calls[0]
    assert "wss://10.0.0.1:443/api/v1/namespaces/overstate/pods/salt-master-0/exec" in call["url"]
    assert "command=%2Fbin%2Fecho" in call["url"]
    assert "command=hi+there" in call["url"]
    assert call["subprotocols"] == ["v4.channel.k8s.io"]
    assert call["header"] == ["Authorization: Bearer test-token"]
    assert call["sslopt"] == {"ca_certs": "/var/run/secrets/ca.crt"}


def test_exec_nonzero_exit_from_status_causes():
    status = _status(
        status="Failure",
        reason="NonZeroExitCode",
        details={"causes": [{"reason": "ExitCode", "message": "2"}]},
    )
    client, _ = _client([b"\x01boom", status])
    out = client.exec_command("p", ["false"])
    assert out["ok"] is False
    assert out["exit_code"] == 2
    assert out["stdout"] == "boom"


def test_exec_closed_stream_without_status_is_success():
    client, _ = _client([b"\x01done"])
    out = client.exec_command("p", ["true"])
    assert out["ok"] is True and out["exit_code"] == 0


def test_exec_container_parameter():
    client, calls = _client([_status()])
    client.exec_command("p", ["x"], container="app")
    assert "container=app" in calls[0]["url"]


def test_exec_unavailable_outside_cluster():
    client = K8sClient(
        config=K8sConfig(server=None, token=None, namespace="x", ca_path=None)
    )
    with pytest.raises(K8sUnavailableError):
        client.exec_command("p", ["x"])


def test_exec_handshake_failure_raises():
    def broken(url, **kwargs):
        raise OSError("no route")

    config = K8sConfig(
        server="https://10.0.0.1:443", token="t", namespace="overstate", ca_path=None
    )
    client = K8sClient(config=config, ws_factory=broken)
    with pytest.raises(K8sError):
        client.exec_command("p", ["x"])

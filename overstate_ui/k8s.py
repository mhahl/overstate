"""Minimal in-cluster Kubernetes client (stdlib only).

Talks to the API server over HTTPS with the pod's ServiceAccount token —
the documented in-pod access pattern — for ConfigMap read/replace,
StatefulSet restart-patch and rollout reads, and pod listing for the
masters status panel.
No third-party dependency; the `transport` hook exists so tests can fake
the API server without touching the network.

Outside a cluster (no token file, no service env) every mutation refuses
via :class:`K8sUnavailableError` naming the equivalent kubectl command.

Concurrency: ConfigMap writes go through PUT-replace carrying the base
``resourceVersion`` the editor form was read at. The API server rejects a
stale base with 409, which becomes :class:`K8sConflictError` — the
stale-base refusal. The restart stamp is a last-write-wins merge patch;
stamping twice is harmless.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


class K8sError(RuntimeError):
    """Base error for Kubernetes API failures. Never carries the token."""


class K8sUnavailableError(K8sError):
    """Raised when mutating from outside a cluster (dev/test machines)."""


class K8sConflictError(K8sError):
    """Raised on HTTP 409: the object changed since it was read."""


class K8sConfig:
    """Connection details. `available` is False outside a cluster."""

    def __init__(
        self,
        server: str | None,
        token: str | None,
        namespace: str,
        ca_path: str | None,
    ) -> None:
        self.server = server
        self.token = token
        self.namespace = namespace
        self.ca_path = ca_path

    @property
    def available(self) -> bool:
        return bool(self.server and self.token)

    @classmethod
    def from_environment(cls) -> K8sConfig:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        server = f"https://{host}:{port}" if host else None
        token = _read_file(os.path.join(SA_DIR, "token"))
        namespace = (
            os.environ.get("K8S_NAMESPACE")
            or _read_file(os.path.join(SA_DIR, "namespace"))
            or "default"
        )
        ca_path = os.path.join(SA_DIR, "ca.crt")
        if not (ca_path and os.path.isfile(ca_path)):
            ca_path = None
        return cls(server, token, namespace.strip(), ca_path)


def _read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _default_transport(
    method: str,
    url: str,
    body: bytes | None,
    token: str,
    ca_path: str | None,
    timeout: float,
) -> tuple[int, dict]:
    """Real HTTPS transport. The token travels in the header only."""
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if body is not None:
        # Merge-patch is PATCH-only; a PUT replace under it fails with
        # HTTP 415 from the API server.
        content_type = (
            "application/merge-patch+json" if method == "PATCH" else "application/json"
        )
        request.add_header("Content-Type", content_type)
    context = ssl.create_default_context(cafile=ca_path) if ca_path else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            payload = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        try:
            message = json.loads(detail).get("message", detail)
        except (ValueError, AttributeError):
            message = detail
        if exc.code == 409:
            raise K8sConflictError(f"object changed since read: {message}")
        raise K8sError(f"kubernetes API HTTP {exc.code}: {message}") from exc
    except OSError as exc:
        raise K8sError(f"kubernetes API unreachable: {exc}") from exc


def _exec_exit_code(status: str) -> int:
    """Exit code from the v4 exec status channel (JSON), 0 when absent."""
    if not status:
        return 0
    try:
        payload = json.loads(status)
    except ValueError:
        return 1
    if payload.get("status") == "Success":
        return 0
    for cause in (payload.get("details") or {}).get("causes") or []:
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message", "1"))
            except (TypeError, ValueError):
                return 1
    return 1


class K8sClient:
    """Namespace-scoped API client. `transport` and `ws_factory` are
    injectable for tests."""

    def __init__(
        self, config: K8sConfig | None = None, transport=None, ws_factory=None
    ) -> None:
        self.config = config or K8sConfig.from_environment()
        self._transport = transport or _default_transport
        self._ws_factory = ws_factory
        self.timeout = 10.0

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.config.available:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        url = f"{self.config.server}{path}"
        assert self.config.token is not None  # narrowed by `available`
        status, payload = self._transport(
            method,
            url,
            raw,
            self.config.token,
            self.config.ca_path,
            self.timeout,
        )
        if status >= 400:
            raise K8sError(f"kubernetes API HTTP {status}")
        return payload

    # -- Pod exec ----------------------------------------------------------

    def exec_command(
        self, pod: str, command: list[str], container: str | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Run ``command`` (argv) in a pod over the exec subresource.

        Returns ``{"ok", "exit_code", "stdout", "stderr"}``; a non-zero
        exit is a result, not an exception. Connection/handshake failures
        raise :class:`K8sError` so callers can tell "command failed" from
        "cannot exec at all".

        Uses the v4.channel.k8s.io websocket protocol (stdout=1,
        stderr=2, status=3). ``ws_factory`` is injectable for tests.
        """
        if not self.config.available:
            raise K8sUnavailableError(
                "not running in a cluster; use kubectl from a workstation"
            )
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover — dependency is pinned
            raise K8sError("websocket-client is not installed") from exc

        server = self.config.server or ""
        ws_base = server.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        params = [("command", arg) for arg in command]
        params += [("stdout", "1"), ("stderr", "1"), ("stdin", "0"), ("tty", "0")]
        if container:
            params.append(("container", container))
        url = (
            f"{ws_base}/api/v1/namespaces/{self.config.namespace}"
            f"/pods/{pod}/exec?{urllib.parse.urlencode(params)}"
        )
        factory = self._ws_factory or websocket.create_connection
        sslopt = {"ca_certs": self.config.ca_path} if self.config.ca_path else {}
        try:
            ws = factory(
                url,
                header=[f"Authorization: Bearer {self.config.token}"],
                sslopt=sslopt,
                subprotocols=["v4.channel.k8s.io"],
                timeout=timeout,
            )
        except Exception as exc:  # socket/TLS/handshake
            raise K8sError("could not open the exec stream") from exc

        stdout: list[bytes] = []
        stderr: list[bytes] = []
        status = ""
        try:
            while True:
                try:
                    frame = ws.recv()
                except Exception:  # noqa: BLE001 — timeout or clean close
                    break
                if not frame:
                    break
                if isinstance(frame, str):
                    frame = frame.encode("utf-8")
                channel, payload = frame[0], frame[1:]
                if channel == 1:
                    stdout.append(payload)
                elif channel == 2:
                    stderr.append(payload)
                elif channel == 3:
                    status = payload.decode("utf-8", "replace")
        finally:
            with contextlib.suppress(Exception):
                ws.close()

        exit_code = _exec_exit_code(status)
        return {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": b"".join(stdout).decode("utf-8", "replace"),
            "stderr": b"".join(stderr).decode("utf-8", "replace"),
        }

    # -- ConfigMaps --------------------------------------------------------

    def get_configmap(self, name: str) -> dict:
        """Return {'data': {...}, 'resourceVersion': '...'}."""
        obj = self._call(
            "GET",
            f"/api/v1/namespaces/{self.config.namespace}/configmaps/{name}",
        )
        return {
            "data": obj.get("data") or {},
            "resourceVersion": (obj.get("metadata") or {}).get("resourceVersion", ""),
        }

    def replace_configmap(
        self, name: str, data: dict, base_resource_version: str
    ) -> str:
        """PUT-replace data; stale base refuses with K8sConflictError.

        Returns the new resourceVersion.
        """
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": self.config.namespace,
                "resourceVersion": base_resource_version,
            },
            "data": data,
        }
        obj = self._call(
            "PUT",
            f"/api/v1/namespaces/{self.config.namespace}/configmaps/{name}",
            body,
        )
        return (obj.get("metadata") or {}).get("resourceVersion", "")

    # -- StatefulSets ------------------------------------------------------

    def restart_statefulset(self, name: str) -> None:
        """Stamp the pod-template restart annotation (rollout restart)."""
        stamped = (
            datetime.datetime.now(datetime.UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        self._call(
            "PATCH",
            f"/apis/apps/v1/namespaces/{self.config.namespace}/statefulsets/{name}",
            {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {RESTART_ANNOTATION: stamped}}
                    }
                }
            },
        )

    def statefulset_rollout(self, name: str) -> dict:
        """Small rollout snapshot: generations and replica counts."""
        obj = self._call(
            "GET",
            f"/apis/apps/v1/namespaces/{self.config.namespace}/statefulsets/{name}",
        )
        meta, status = obj.get("metadata") or {}, obj.get("status") or {}
        spec = obj.get("spec") or {}
        return {
            "observedGeneration": status.get("observedGeneration"),
            "generation": meta.get("generation"),
            "replicas": spec.get("replicas"),
            "readyReplicas": status.get("readyReplicas", 0),
            "updatedReplicas": status.get("updatedReplicas", 0),
        }

    def list_pods(self, label_selector: str) -> list[dict]:
        """Per-pod rows for status panels: name, phase, readiness,
        container image, and restart count. JSON-serializable."""
        obj = self._call(
            "GET",
            f"/api/v1/namespaces/{self.config.namespace}/pods"
            f"?labelSelector={label_selector}",
        )
        rows = []
        for pod in obj.get("items") or []:
            meta, status = pod.get("metadata") or {}, pod.get("status") or {}
            containers = status.get("containerStatuses") or []
            images = sorted({c.get("image", "") for c in containers} - {""})
            ready_conds = [
                c.get("status") == "True"
                for c in status.get("conditions") or []
                if c.get("type") == "Ready"
            ]
            rows.append(
                {
                    "name": meta.get("name", ""),
                    "phase": status.get("phase", ""),
                    "ready": bool(containers)
                    and all(c.get("ready") for c in containers)
                    and (not ready_conds or all(ready_conds)),
                    "images": images,
                    "restarts": sum(c.get("restartCount", 0) for c in containers),
                }
            )
        return rows

    def pods_ready(self, label_selector: str) -> tuple[int, int]:
        """(ready, total) pods matching a selector in this namespace."""
        obj = self._call(
            "GET",
            f"/api/v1/namespaces/{self.config.namespace}/pods"
            f"?labelSelector={label_selector}",
        )
        items = obj.get("items") or []
        ready = sum(
            1
            for pod in items
            if all(
                c.get("status") == "True"
                for c in (pod.get("status") or {}).get("conditions") or []
                if c.get("type") == "Ready"
            )
        )
        return ready, len(items)

    def create_pod(self, manifest: dict) -> dict:
        """Create a Pod in this namespace."""
        manifest.setdefault("apiVersion", "v1")
        manifest.setdefault("kind", "Pod")
        meta = manifest.setdefault("metadata", {})
        meta["namespace"] = self.config.namespace
        return self._call(
            "POST",
            f"/api/v1/namespaces/{self.config.namespace}/pods",
            manifest,
        )

    def get_pod(self, name: str) -> dict | None:
        """Get Pod resource or None if not found."""
        try:
            return self._call(
                "GET",
                f"/api/v1/namespaces/{self.config.namespace}/pods/{name}",
            )
        except K8sError as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise

    def delete_pod(self, name: str, grace_period_seconds: int = 0) -> dict | None:
        """Delete a Pod in this namespace."""
        try:
            return self._call(
                "DELETE",
                f"/api/v1/namespaces/{self.config.namespace}/pods/{name}?gracePeriodSeconds={grace_period_seconds}",
            )
        except K8sError as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise

    def cleanup_salt_cli_pods(self, max_age_seconds: int = 1800) -> list[str]:
        """Delete completed, failed, deadlined, or stale salt-cli pods."""
        cleaned = []
        try:
            res = self._call(
                "GET",
                f"/api/v1/namespaces/{self.config.namespace}/pods?labelSelector=app.kubernetes.io/name=salt-cli",
            )
            now = datetime.datetime.now(datetime.UTC)
            for item in res.get("items") or []:
                meta = item.get("metadata") or {}
                name = meta.get("name", "")
                status = item.get("status") or {}
                phase = status.get("phase", "")
                reason = status.get("reason", "")
                created_str = meta.get("creationTimestamp", "")
                is_expired = False
                if created_str:
                    try:
                        created = datetime.datetime.fromisoformat(
                            created_str.replace("Z", "+00:00")
                        )
                        if (now - created).total_seconds() > max_age_seconds:
                            is_expired = True
                    except Exception:
                        pass
                if phase in ("Succeeded", "Failed") or is_expired or reason == "DeadlineExceeded":
                    try:
                        self.delete_pod(name)
                        cleaned.append(name)
                    except Exception:
                        pass
        except Exception:
            pass
        return cleaned

    def exec_pod(
        self,
        pod_name: str,
        command: list[str],
        container: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """Execute command in pod via Kubernetes WebSocket exec.

        Returns (exit_code, stdout, stderr).
        """
        if not self.config.available:
            raise K8sUnavailableError("not running in a cluster")

        if hasattr(self, "_exec_transport") and callable(self._exec_transport):
            return self._exec_transport(pod_name, command, container, timeout)

        query = [("stdout", "true"), ("stderr", "true")]
        if container:
            query.append(("container", container))
        for cmd in command:
            query.append(("command", cmd))
        qs = urllib.parse.urlencode(query)

        parsed = urllib.parse.urlparse(self.config.server)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/api/v1/namespaces/{self.config.namespace}/pods/{pod_name}/exec?{qs}"

        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer {self.config.token}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: v4.channel.k8s.io\r\n\r\n"
        )

        ctx = None
        if parsed.scheme == "https":
            if self.config.ca_path and os.path.isfile(self.config.ca_path):
                ctx = ssl.create_default_context(cafile=self.config.ca_path)
            else:
                ctx = ssl._create_unverified_context()

        raw_sock = socket.create_connection((host, port), timeout=timeout)
        sock = ctx.wrap_socket(raw_sock, server_hostname=host) if ctx else raw_sock
        sock.settimeout(timeout)
        try:
            sock.sendall(req.encode("ascii"))
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                header_data += chunk
            if b"101" not in header_data.split(b"\r\n")[0]:
                raise K8sError(
                    f"WebSocket exec handshake failed: {header_data[:200].decode('utf-8', 'replace')}"
                )

            rest = header_data.split(b"\r\n\r\n", 1)[1]
            buf = rest
            stdout_parts = []
            stderr_parts = []
            status_parts = []

            while True:
                while len(buf) < 2:
                    c = sock.recv(4096)
                    if not c:
                        break
                    buf += c
                if len(buf) < 2:
                    break
                b1, b2 = buf[0], buf[1]
                opcode = b1 & 0x0F
                has_mask = (b2 & 0x80) != 0
                length = b2 & 0x7F
                idx = 2
                if length == 126:
                    while len(buf) < idx + 2:
                        c = sock.recv(4096)
                        if not c:
                            break
                        buf += c
                    if len(buf) < idx + 2:
                        break
                    length = int.from_bytes(buf[idx : idx + 2], "big")
                    idx += 2
                elif length == 127:
                    while len(buf) < idx + 8:
                        c = sock.recv(4096)
                        if not c:
                            break
                        buf += c
                    if len(buf) < idx + 8:
                        break
                    length = int.from_bytes(buf[idx : idx + 8], "big")
                    idx += 8

                mask = None
                if has_mask:
                    while len(buf) < idx + 4:
                        c = sock.recv(4096)
                        if not c:
                            break
                        buf += c
                    if len(buf) < idx + 4:
                        break
                    mask = buf[idx : idx + 4]
                    idx += 4

                while len(buf) < idx + length:
                    c = sock.recv(4096)
                    if not c:
                        break
                    buf += c
                if len(buf) < idx + length:
                    break

                payload = buf[idx : idx + length]
                buf = buf[idx + length :]
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

                if opcode == 8:  # close
                    break
                if opcode in (1, 2) and payload:
                    channel = payload[0]
                    data = payload[1:]
                    if channel == 1:
                        stdout_parts.append(data)
                    elif channel == 2:
                        stderr_parts.append(data)
                    elif channel == 3:
                        status_parts.append(data)
        finally:
            sock.close()

        stdout = b"".join(stdout_parts).decode("utf-8", "replace")
        stderr = b"".join(stderr_parts).decode("utf-8", "replace")
        status_raw = b"".join(status_parts).decode("utf-8", "replace")

        exit_code = 0
        if status_raw:
            try:
                st = json.loads(status_raw)
                if st.get("status") == "Failure":
                    causes = (st.get("details") or {}).get("causes") or []
                    exit_code = 1
                    for c in causes:
                        if c.get("reason") == "ExitCode":
                            try:
                                exit_code = int(c.get("message", 1))
                            except ValueError:
                                exit_code = 1
                elif st.get("status") == "Success":
                    exit_code = 0
            except Exception:
                pass

        return exit_code, stdout, stderr

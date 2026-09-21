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

import datetime
import json
import os
import ssl
import urllib.error
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


class K8sClient:
    """Namespace-scoped API client. `transport` is injectable for tests."""

    def __init__(self, config: K8sConfig | None = None, transport=None) -> None:
        self.config = config or K8sConfig.from_environment()
        self._transport = transport or _default_transport
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

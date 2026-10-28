"""Failover-cluster addressing (D12: full active-active).

Every publish fans out to all master pods because publish buses are
per-master: a job fired on one pod never reaches minions attached to
the other. Pods are addressed by stable StatefulSet DNS through the
headless service, so the list survives reschedules. Outside a cluster
(dev) or when the API is unreachable, callers fall back to the single
default client and behavior is unchanged.
"""

from __future__ import annotations

import threading
import time

import httpx
from flask import current_app

from .k8s import K8sClient, K8sError
from .salt_client import SaltApiError, SaltClient

POD_URLS_TTL = 60.0
"""How long one K8s API answer for the master pods is trusted. Replica
count changes almost never; without this every web request pays a
K8s round trip before it even reaches Salt."""

_pod_urls_cache: dict = {"urls": [], "at": 0.0}
_pod_urls_lock = threading.Lock()

_client_cache: dict = {}
_client_lock = threading.Lock()


def pod_api_urls() -> list[str]:
    """salt-api base URLs, one per master pod. Empty when unavailable.

    The K8s answer is cached per process for POD_URLS_TTL. When the API
    call fails, the last-known list is reused instead of silently
    collapsing to single-master: a stale pod shows up as an unreachable
    warning on the next fan-out, which beats quietly half-applying.
    """
    now = time.monotonic()
    with _pod_urls_lock:
        cached = list(_pod_urls_cache["urls"])
        fresh = now - _pod_urls_cache["at"] < POD_URLS_TTL
    if fresh and cached:
        return cached
    try:
        client = K8sClient()
        if not client.config.available:
            return []
        sts = current_app.config["MASTER_STATEFULSET"]
        replicas = client.statefulset_rollout(sts)["replicas"] or 1
        headless = current_app.config["MASTER_HEADLESS_SERVICE"]
        namespace = client.config.namespace
    except K8sError:
        return cached
    urls = [
        f"https://{sts}-{i}.{headless}.{namespace}.svc.cluster.local:8000"
        for i in range(replicas)
    ]
    with _pod_urls_lock:
        _pod_urls_cache["urls"] = urls
        _pod_urls_cache["at"] = now
    return urls


def pod_clients(default: SaltClient) -> list[tuple[str, SaltClient]]:
    """(pod name, client) per master; [(master, default)] as fallback.

    Pod clients are cached per process and shared: a fresh client costs
    a new connection pool, TLS handshake, and eauth login on first use,
    so rebuilding them per request multiplies every fan-out. Sharing is
    safe because SaltClient re-logins on 401 by itself.
    """
    urls = pod_api_urls()
    if not urls:
        return [("master", default)]
    config = current_app.config
    creds = (
        config["SALT_EAUTH_USER"],
        config["SALT_EAUTH_PASSWORD"],
        config["SALT_EAUTH_TYPE"],
        config["SALT_API_VERIFY"],
    )
    out = []
    for i, url in enumerate(urls):
        key = (url,) + tuple(creds)
        client = _client_cache.get(key)
        if client is None:
            with _client_lock:
                client = _client_cache.get(key)
                if client is None:
                    user, password, eauth, verify = creds
                    client = SaltClient(url, user, password, eauth, verify=verify)
                    _client_cache[key] = client
        out.append((f"pod-{i}", client))
    return out


def drop_client_cache() -> None:
    """Forget cached pod clients and pod URLs. Tests and credential
    rotation only; request paths never call this."""
    with _client_lock:
        _client_cache.clear()
    with _pod_urls_lock:
        _pod_urls_cache["urls"] = []
        _pod_urls_cache["at"] = 0.0


POD_MAP_HTTP_TIMEOUT = 8.0


def fleet_pod_map(default: SaltClient, http_timeout: float = POD_MAP_HTTP_TIMEOUT) -> dict:
    """{minion_id: master pod name holding its live connection}.

    Each master's ``manage.status`` lists only the minions attached to
    it, so the union is a minion->pod map for routing single-minion
    live calls to the owning master instead of the load-balanced VIP
    (where ClientIP affinity can pin the app to a master that does not
    own the minion). Held in a short Redis cache; a failover re-owning
    shows up within the TTL. Empty when every pod is silent.
    """
    from .tasks_queue import read_pod_map_cache, write_pod_map_cache

    cached = read_pod_map_cache()
    if isinstance(cached, dict) and cached:
        return cached
    acc: dict = {}
    answered = False
    for name, cli in pod_clients(default):
        try:
            status = cli.runner(
                "manage.status",
                http_timeout=http_timeout,
                timeout=1,
                gather_job_timeout=1,
            )[0]
        except (SaltApiError, httpx.HTTPError, KeyError, IndexError, TypeError):
            continue
        if not isinstance(status, dict):
            continue
        answered = True
        for mid in status.get("up") or []:
            acc.setdefault(str(mid), name)
    if answered and acc:
        write_pod_map_cache(acc)
    return acc


def local_one_raw(
    default: SaltClient,
    mid: str,
    fun: str,
    arg: list | None = None,
    kwarg: dict | None = None,
    via: str = "local",
    timeout: int = 10,
    http_timeout: float | None = None,
):
    """Like :func:`local_one` but returns the raw salt-api return list,
    for callers that inspect the response shape itself (e.g. beacon
    refusal payloads). Routes to the owning pod when known; falls back
    to the default client otherwise."""
    mapping = fleet_pod_map(default)
    clients = dict(pod_clients(default))
    cli = clients.get(mapping.get(mid, "")) if mapping else None
    if cli is None:
        cli = default
    return cli.local(
        mid,
        fun,
        arg=arg or [],
        tgt_type="list",
        via=via,
        timeout=timeout,
        kwarg=kwarg,
        http_timeout=http_timeout,
    )


def local_one(
    default: SaltClient,
    mid: str,
    fun: str,
    arg: list | None = None,
    kwarg: dict | None = None,
    via: str = "local",
    timeout: int = 10,
    http_timeout: float | None = None,
):
    """Run ``fun`` on a single minion via its owning master pod.

    Returns the ``[0].get(mid)`` payload like a plain synchronous
    local call. Falls back to the default client when the pod map is
    unavailable (dev box, all pods silent) or the minion's owner is
    unknown — so behavior outside a cluster is unchanged. Raises
    SaltApiError/httpx errors to the caller exactly like a direct
    ``client.local`` call.
    """
    mapping = fleet_pod_map(default)
    clients = dict(pod_clients(default))
    cli = clients.get(mapping.get(mid, "")) if mapping else None
    if cli is None:
        cli = default
    return cli.local(
        mid,
        fun,
        arg=arg or [],
        tgt_type="list",
        via=via,
        timeout=timeout,
        kwarg=kwarg,
        http_timeout=http_timeout,
    )[0].get(mid)

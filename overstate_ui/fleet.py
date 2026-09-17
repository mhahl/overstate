"""Failover-cluster addressing (D12: full active-active).

Every publish fans out to all master pods because publish buses are
per-master: a job fired on one pod never reaches minions attached to
the other. Pods are addressed by stable StatefulSet DNS through the
headless service, so the list survives reschedules. Outside a cluster
(dev) or when the API is unreachable, callers fall back to the single
default client and behavior is unchanged.
"""

from __future__ import annotations

from flask import current_app

from .k8s import K8sClient, K8sError
from .salt_client import SaltClient


def pod_api_urls() -> list[str]:
    """salt-api base URLs, one per master pod. Empty when unavailable."""
    try:
        client = K8sClient()
        if not client.config.available:
            return []
        sts = current_app.config["MASTER_STATEFULSET"]
        replicas = client.statefulset_rollout(sts)["replicas"] or 1
        headless = current_app.config["MASTER_HEADLESS_SERVICE"]
        namespace = client.config.namespace
    except K8sError:
        return []
    return [
        f"https://{sts}-{i}.{headless}.{namespace}.svc.cluster.local:8000"
        for i in range(replicas)
    ]


def pod_clients(default: SaltClient) -> list[tuple[str, SaltClient]]:
    """(pod name, client) per master; [(master, default)] as fallback."""
    urls = pod_api_urls()
    if not urls:
        return [("master", default)]
    config = current_app.config
    return [
        (
            f"pod-{i}",
            SaltClient(
                url,
                config["SALT_EAUTH_USER"],
                config["SALT_EAUTH_PASSWORD"],
                config["SALT_EAUTH_TYPE"],
                verify=config["SALT_API_VERIFY"],
            ),
        )
        for i, url in enumerate(urls)
    ]

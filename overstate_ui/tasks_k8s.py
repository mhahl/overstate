"""Kubernetes status probes run inline or on RQ workers.

Same contract as :mod:`overstate_ui.tasks_salt`: ``*_now`` takes an
explicit client plus object names (testable without an app), and the
``*_task`` sibling builds an isolated app and delegates. Results must
stay JSON-serializable — RQ persists them to Redis.
"""

from __future__ import annotations

from typing import Any

from .k8s import K8sClient, K8sError, K8sUnavailableError
from .tasks_queue import isolated_app

MASTER_LABEL = "app.kubernetes.io/name=salt-master"
"""Pod selector for the salt-master StatefulSet (matches its
matchLabels, so it survives kustomize label additions)."""


def _rollout_complete(rollout: dict) -> bool:
    """Settled generation with every replica updated and ready."""
    if rollout.get("generation") != rollout.get("observedGeneration"):
        return False
    replicas = rollout.get("replicas")
    if replicas is None:
        return False
    return (
        rollout.get("updatedReplicas", 0) == rollout.get("readyReplicas", 0) == replicas
    )


def _short_image(image: str) -> str:
    """Registry-stripped repo:tag for narrow panels."""
    return image.split("/")[-1] if image else "—"


def master_status_now(
    client: K8sClient, sts_name: str, configmap_name: str
) -> dict[str, Any]:
    """Master rollout + per-pod rows + live config revision.

    Raises :class:`K8sError` when the API server refuses; callers
    translate that into their own unavailable shape.
    """
    rollout = client.statefulset_rollout(sts_name)
    pods = []
    for pod in client.list_pods(MASTER_LABEL):
        images = pod.get("images") or []
        pods.append(
            {
                "name": pod.get("name", ""),
                "phase": pod.get("phase", ""),
                "ready": bool(pod.get("ready")),
                "image": images[0] if images else "",
                "image_short": _short_image(images[0]) if images else "—",
                "restarts": pod.get("restarts", 0),
            }
        )
    try:
        revision = client.get_configmap(configmap_name)["resourceVersion"] or None
    except K8sError:
        revision = None
    distinct = {p["image"] for p in pods if p["image"]}
    return {
        "available": True,
        "sts": sts_name,
        "complete": _rollout_complete(rollout),
        "replicas": rollout.get("replicas"),
        "ready_replicas": rollout.get("readyReplicas", 0),
        "updated_replicas": rollout.get("updatedReplicas", 0),
        "ready_pods": sum(1 for p in pods if p["ready"]),
        "pods": pods,
        "images_differ": len(distinct) > 1,
        "config_revision": revision,
    }


def master_status_task() -> dict[str, Any]:
    """RQ probe for the dashboard masters panel. Outside a cluster
    (no ServiceAccount) this reports unavailable instead of failing,
    so dev machines render the fallback rather than a dead probe."""
    from flask import current_app

    with isolated_app():
        try:
            client = K8sClient()
            if not client.config.available:
                return {"available": False}
            return master_status_now(
                client,
                current_app.config["MASTER_STATEFULSET"],
                current_app.config["MASTER_CONFIGMAP"],
            )
        except K8sUnavailableError:
            return {"available": False}

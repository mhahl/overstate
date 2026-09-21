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


def mastercheck_now(salt=None, k8s=None) -> dict[str, Any]:
    """Observability checklist: salt-api login, masters rollout,
    returner freshness, capability cache. Each item reports
    ok/skipped/failing independently and the whole check never raises,
    so a dead master still yields a readable card. All values stay
    JSON-serializable for the RQ result store. Pass explicit clients in
    tests; production builds its own."""
    import datetime as _dt

    from flask import current_app

    from .dashboard import returns_health
    from .tasks_queue import read_capability_cache
    from .tasks_salt import CAPABILITY_CHECKS

    items: list[dict[str, str]] = []

    def add(key: str, label: str, state: str, detail: str = "") -> None:
        items.append({"key": key, "label": label, "state": state, "detail": detail})

    if salt is None:
        from .tasks_queue import build_client

        try:
            salt = build_client()
        except Exception:  # noqa: BLE001 — no client configured
            salt = None
    if salt is None:
        add("salt-api", "salt-api login", "skipped", "no Salt client configured")
    else:
        try:
            salt.login(http_timeout=15.0)
        except Exception:  # noqa: BLE001 — any failure means unhealthy
            add("salt-api", "salt-api login", "failing", "login refused")
        else:
            add("salt-api", "salt-api login", "ok")

    if k8s is None:
        k8s = K8sClient()
    if not k8s.config.available:
        add("rollout", "Masters rollout", "skipped", "no cluster from here")
    else:
        try:
            rollout = k8s.statefulset_rollout(current_app.config["MASTER_STATEFULSET"])
        except K8sError:
            add("rollout", "Masters rollout", "failing", "API refused")
        else:
            ready = rollout.get("readyReplicas", 0) or 0
            wanted = rollout.get("replicas") or 0
            if _rollout_complete(rollout):
                add("rollout", "Masters rollout", "ok", f"{ready}/{wanted} ready")
            else:
                add("rollout", "Masters rollout", "failing", f"{ready}/{wanted} ready")

    try:
        health = returns_health()
    except Exception:  # noqa: BLE001 — an unreadable store is a finding
        health = None
    if health is None:
        add("returner", "Returner fresh", "failing", "return store unreadable")
    elif health["stale"]:
        add("returner", "Returner fresh", "failing", "jobs completed, nothing stored")
    elif health["age"] is None:
        add("returner", "Returner fresh", "ok", "nothing run yet")
    else:
        add("returner", "Returner fresh", "ok", f"last return {health['age']}")

    cached = read_capability_cache()
    if cached is None:
        add("capabilities", "Capability check", "failing", "no check yet")
    else:
        bad = [c["feature"] for c in CAPABILITY_CHECKS if not cached.get(c["key"])]
        if bad:
            add(
                "capabilities",
                "Capability check",
                "failing",
                f"{len(bad)} door(s) failing",
            )
        else:
            add("capabilities", "Capability check", "ok", "all doors ok")

    failing = sum(1 for item in items if item["state"] == "failing")
    return {
        "items": items,
        "failing": failing,
        "checked_at": _dt.datetime.now(_dt.UTC).isoformat(),
    }


def mastercheck_task() -> dict[str, Any]:
    """RQ checklist probe for the Master Config card. Caches with the
    shared capability TTL; sync fallback in the route runs
    :func:`mastercheck_now` inline."""
    with isolated_app():
        from .tasks_queue import write_mastercheck_cache

        out = mastercheck_now()
        write_mastercheck_cache(out)
        return out


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

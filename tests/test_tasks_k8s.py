"""Master-status probe tests: rollout math, pod shaping, and the
outside-a-cluster unavailable shape."""

from contextlib import nullcontext

import pytest

from overstate_ui.k8s import K8sError
from overstate_ui.tasks_k8s import _rollout_complete, master_status_now

IMAGE = "quay.io/sigaint/overstate-salt-master:lts-pg1"


class FakeK8s:
    def __init__(self, rollout, pods, revision="4242", config_error=False):
        self.rollout = rollout
        self.pods = pods
        self.revision = revision
        self.config_error = config_error

    def statefulset_rollout(self, name):
        assert name == "salt-master"
        return self.rollout

    def list_pods(self, selector):
        assert selector == "app.kubernetes.io/name=salt-master"
        return self.pods

    def get_configmap(self, name):
        assert name == "salt-master-config"
        if self.config_error:
            raise K8sError("forbidden")
        return {"data": {}, "resourceVersion": self.revision}


def _rollout(**over):
    base = {
        "observedGeneration": 4,
        "generation": 4,
        "replicas": 2,
        "readyReplicas": 2,
        "updatedReplicas": 2,
    }
    base.update(over)
    return base


def _pod(name, ready=True, image=IMAGE, restarts=0):
    return {
        "name": name,
        "phase": "Running",
        "ready": ready,
        "images": [image],
        "restarts": restarts,
    }


def test_complete_rollout_shapes_panel():
    out = master_status_now(
        FakeK8s(_rollout(), [_pod("salt-master-0"), _pod("salt-master-1")]),
        "salt-master",
        "salt-master-config",
    )
    assert out["available"] is True
    assert out["complete"] is True
    assert out["ready_pods"] == 2
    assert out["images_differ"] is False
    assert out["config_revision"] == "4242"
    assert out["pods"][0]["image_short"] == "overstate-salt-master:lts-pg1"


def test_generation_behind_is_progressing():
    out = master_status_now(
        FakeK8s(_rollout(observedGeneration=3), [_pod("salt-master-0")]),
        "salt-master",
        "salt-master-config",
    )
    assert out["complete"] is False
    assert out["ready_pods"] == 1


def test_stale_replica_is_progressing():
    out = master_status_now(
        FakeK8s(_rollout(updatedReplicas=1, readyReplicas=1), [_pod("salt-master-0")]),
        "salt-master",
        "salt-master-config",
    )
    assert out["complete"] is False


def test_differing_images_flagged():
    out = master_status_now(
        FakeK8s(
            _rollout(),
            [_pod("salt-master-0"), _pod("salt-master-1", image=IMAGE + "-new")],
        ),
        "salt-master",
        "salt-master-config",
    )
    assert out["images_differ"] is True


def test_config_read_failure_keeps_panel():
    out = master_status_now(
        FakeK8s(_rollout(), [_pod("salt-master-0")], config_error=True),
        "salt-master",
        "salt-master-config",
    )
    assert out["available"] is True
    assert out["config_revision"] is None


@pytest.mark.parametrize(
    "rollout,complete",
    [
        (_rollout(), True),
        (_rollout(observedGeneration=3), False),
        (_rollout(updatedReplicas=1), False),
        (_rollout(readyReplicas=1), False),
        (_rollout(replicas=None), False),
        (_rollout(replicas=0, updatedReplicas=0, readyReplicas=0), True),
    ],
)
def test_rollout_math(rollout, complete):
    assert _rollout_complete(rollout) is complete


def test_task_reports_unavailable_off_cluster(monkeypatch):
    import overstate_ui.tasks_k8s as tasks_k8s_mod

    class NoCluster:
        config = type("Config", (), {"available": False})()

    monkeypatch.setattr(tasks_k8s_mod, "K8sClient", lambda: NoCluster())
    monkeypatch.setattr(tasks_k8s_mod, "isolated_app", lambda: nullcontext())
    assert tasks_k8s_mod.master_status_task() == {"available": False}

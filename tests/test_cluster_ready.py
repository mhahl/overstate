"""cluster-ready.py: Raft-leader gate for salt-master readiness."""

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
READY_PY = REPO / "cluster-ready.py"


def _load():
    spec = importlib.util.spec_from_file_location("cluster_ready", READY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SELF = "salt-master-0.salt-master"
OTHER = "salt-master-1.salt-master"


def test_founder_leader_is_ready_even_with_empty_voters():
    mod = _load()
    assert mod.is_ready({"leader_id": SELF, "voters": [], "learners": []}, SELF)


def test_joiner_needs_membership_under_a_leader():
    mod = _load()
    assert not mod.is_ready({"leader_id": OTHER, "voters": [], "learners": []}, SELF)
    assert mod.is_ready(
        {"leader_id": OTHER, "voters": [OTHER, SELF], "learners": []}, SELF
    )
    assert mod.is_ready(
        {"leader_id": OTHER, "voters": [OTHER], "learners": [SELF]}, SELF
    )


def test_null_leader_is_never_ready():
    mod = _load()
    assert not mod.is_ready(
        {"leader_id": None, "voters": [SELF], "learners": []}, SELF
    )
    assert not mod.is_ready({}, SELF)
    assert not mod.is_ready(None, SELF)

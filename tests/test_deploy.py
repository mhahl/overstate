"""Deploy artifact tests: the Kubernetes manifests stay consistent."""

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
K8S = REPO / "deploy" / "kubernetes"


def _load(name):
    with open(K8S / name, encoding="utf-8") as fh:
        return list(yaml.safe_load_all(fh))


def test_kustomization_wires_history_configmap():
    text = (K8S / "kustomization.yaml").read_text()
    assert "salt-master-config-history.yaml" in text


def _cronjob():
    (cj,) = [d for d in _load("reconcile-cronjob.yaml") if d.get("kind") == "CronJob"]
    return cj


def test_reconcile_cronjob_wired_and_bounded():
    assert "reconcile-cronjob.yaml" in (K8S / "kustomization.yaml").read_text()
    cj = _cronjob()
    assert cj["spec"]["schedule"] == "7 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    # Same codebase as the app; the kustomize images transformer
    # rewrites the placeholder at build time.
    assert container["image"] == "overstate-app"
    assert container["command"] == ["python", "-m", "overstate_ui.reconcile_cli"]
    env = {e["name"]: e for e in container["env"]}
    assert env["SALT_API_URL"]["value"].startswith("https://")
    assert env["SALT_EAUTH_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == (
        "overstate-secrets"
    )
    assert "ADMIN_PASSWORD" not in env  # least privilege: no UI login seed


def test_history_configmap_deploys_empty_and_managed():
    (doc,) = _load("salt-master-config-history.yaml")
    assert doc["kind"] == "ConfigMap"
    assert doc["metadata"]["name"] == "salt-master-config-history"
    assert (doc.get("data") or {}) == {}
    assert (
        doc["metadata"]["annotations"]["overstate.sigaint.au/managed-by"]
        == "overstate-app"
    )


def _app_role():
    (role,) = [d for d in _load("rbac.yaml") if d.get("kind") == "Role"]
    return role


def test_app_role_has_no_cluster_scope_and_no_configmap_delete():
    role = _app_role()
    assert role["metadata"]["name"] == "overstate-app"
    cm_rules = [r for r in role["rules"] if "configmaps" in r.get("resources", [])]
    assert cm_rules, "app Role must govern owned ConfigMaps"
    for rule in cm_rules:
        assert "delete" not in rule.get("verbs", [])
        assert set(rule.get("resourceNames", [])) == {
            "salt-master-config",
            "salt-master-config-history",
        }
    sts_rules = [r for r in role["rules"] if "statefulsets" in r.get("resources", [])]
    for rule in sts_rules:
        assert "delete" not in rule.get("verbs", [])
        assert rule.get("resourceNames") == ["salt-master"]


def test_rbac_has_no_cluster_scoped_kinds():
    text = (K8S / "rbac.yaml").read_text()
    assert "ClusterRole" not in text


def _salt_master_sts():
    (sts,) = [d for d in _load("salt-master.yaml") if d.get("kind") == "StatefulSet"]
    return sts


def test_master_pair_never_shares_a_node():
    sts = _salt_master_sts()
    required = sts["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert {
        "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "salt-master"}},
        "topologyKey": "kubernetes.io/hostname",
    } in required


def test_master_pair_keeps_one_pod_on_drain():
    (pdb,) = [
        d for d in _load("salt-master.yaml") if d.get("kind") == ("PodDisruptionBudget")
    ]
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "salt-master"
    }


def _app_deployment():
    (dep,) = [d for d in _load("app.yaml") if d.get("kind") == "Deployment"]
    return dep


def test_app_replicas_prefer_separate_nodes():
    dep = _app_deployment()
    assert dep["spec"]["replicas"] == 2
    preferred = dep["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"
    ]
    assert {
        "weight": 100,
        "podAffinityTerm": {
            "labelSelector": {
                "matchLabels": {"app.kubernetes.io/name": "overstate-app"}
            },
            "topologyKey": "kubernetes.io/hostname",
        },
    } in preferred


def test_app_keeps_one_replica_on_drain():
    (pdb,) = [d for d in _load("app.yaml") if d.get("kind") == "PodDisruptionBudget"]
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "overstate-app"
    }


def test_master_pair_replicas_and_image():
    sts = _salt_master_sts()
    assert sts["spec"]["replicas"] == 2
    containers = sts["spec"]["template"]["spec"]["containers"]
    (master,) = [c for c in containers if c["name"] == "salt-master"]
    assert master["image"].startswith("quay.io/sigaint/overstate:")
    assert "salt-master" in master["image"]
    assert "ghcr.io" not in master["image"]


def test_master_pair_shares_keypair_but_not_accepted_keys():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["master-keypair"]["secret"]["secretName"] == "salt-master-keys"
    assert "keys" not in volumes  # accepted keys ride the claim template
    templates = {t["metadata"]["name"] for t in sts["spec"]["volumeClaimTemplates"]}
    assert templates == {"keys"}
    (master,) = [c for c in pod["containers"] if c["name"] == "salt-master"]
    mounts = {
        m["mountPath"]: m for m in master["volumeMounts"] if "keys" in m["mountPath"]
    }
    assert mounts["/home/salt/data/keys/master.pem"]["subPath"] == "master.pem"
    assert mounts["/home/salt/data/keys/master.pub"]["subPath"] == "master.pub"


def test_keypair_secret_is_owner_created_not_committed():
    text = (K8S / "kustomization.yaml").read_text()
    assert "salt-master-keys" not in text
    assert not (K8S / "salt-master-keys.yaml").exists()


def test_app_role_grants_no_secret_access():
    role = _app_role()
    assert "secrets" not in [
        r for rule in role["rules"] for r in rule.get("resources", [])
    ]


def test_keypair_secret_readable_by_salt_user():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == 1000
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["master-keypair"]["secret"]["defaultMode"] == 0o440


def test_returner_credentials_ride_a_secret_not_the_configmap():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    sources = volumes["config"]["projected"]["sources"]
    kinds = set()
    for source in sources:
        kind, ref = next(iter(source.items()))
        kinds.add((kind, ref.get("name")))
    assert ("configMap", "salt-master-config") in kinds
    assert ("secret", "salt-master-db") in kinds
    assert "master-db" not in volumes  # no separate secret volume
    (master,) = [c for c in pod["containers"] if c["name"] == "salt-master"]
    for mount in master["volumeMounts"]:
        if mount["mountPath"].startswith("/home/salt/data/config/"):
            assert mount.get("subPath") is None, (
                "subPath file mounts into the read-only config dir fail "
                "with ENOTDIR on these nodes; use the projected volume"
            )
    text = (K8S / "salt-master-config.yaml").read_text()
    assert not re.search(r"(?m)^\s*(passwd|password)\s*:", text)
    assert "salt-master-db" not in (K8S / "kustomization.yaml").read_text()

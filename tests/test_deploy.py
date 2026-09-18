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
    (role,) = [
        d
        for d in _load("rbac.yaml")
        if d.get("kind") == "Role"
        and d.get("metadata", {}).get("name") == "overstate-app"
    ]
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


def test_master_trio_never_shares_a_node():
    sts = _salt_master_sts()
    required = sts["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert {
        "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "salt-master"}},
        "topologyKey": "kubernetes.io/hostname",
    } in required


def test_master_raft_timeouts_fit_cross_node_k8s():
    import yaml as _yaml

    (cm,) = [
        d for d in _load("salt-master-config.yaml") if d.get("kind") == "ConfigMap"
    ]
    master_conf = _yaml.safe_load(cm["data"]["master.conf"])
    # Followers must tolerate multi-second beacon gaps (50-100 ms
    # beacons): tight timeouts phase-locked our voters into a ~1 Hz
    # duel of spurious elections with no stable leader.
    assert master_conf["cluster_election_min"] >= 3000
    assert master_conf["cluster_election_max"] >= 6000
    assert master_conf.get("keys.cache_driver") == "localfs_key"
    include = master_conf.get("include") or []
    if isinstance(include, str):
        include = [include]
    assert "/home/salt/data/keys/cluster-identity.conf" in include


def test_master_image_always_pulls_floating_tag():
    sts = _salt_master_sts()
    (container,) = sts["spec"]["template"]["spec"]["containers"]
    assert container["image"] == "quay.io/sigaint/overstate-salt-master:lts-pg13"
    # Same-tag rebuilds (entrypoint fixes) must reach the nodes.
    assert container["imagePullPolicy"] == "Always"


def _master_container():
    sts = _salt_master_sts()
    (container,) = sts["spec"]["template"]["spec"]["containers"]
    return container


def test_master_liveness_checks_daemon_not_health():
    probe = _master_container()["livenessProbe"]["exec"]["command"]
    text = " ".join(probe)
    # Process-existence only: a clustered-but-uncommitted master is
    # alive and must not be restarted for it.
    assert "supervisorctl" in text
    assert "salt-master" in text


def test_api_service_sticks_clients_to_one_master():
    (svc,) = [
        d
        for d in _load("salt-master.yaml")
        if d.get("kind") == "Service" and d.get("metadata", {}).get("name") == "salt-master-api"
    ]
    # Eauth tokens live on the minting pod: without affinity every
    # flap round-robins clients into 401s.
    assert svc["spec"].get("sessionAffinity") == "ClientIP"


def test_mq_service_sticks_minions_to_one_master():
    (svc,) = [
        d
        for d in _load("salt-master.yaml")
        if d.get("kind") == "Service" and d.get("metadata", {}).get("name") == "salt-master-mq"
    ]
    # Per-node pin: Traefik on a node only reaches the local master.
    assert svc["spec"].get("internalTrafficPolicy") == "Local"
    assert svc["spec"].get("sessionAffinity") == "ClientIP"
    assert (
        svc["spec"]["sessionAffinityConfig"]["clientIP"]["timeoutSeconds"] == 86400
    )


def test_mq_traefik_routes_use_native_lb():
    docs = _load("salt-mq-routes.yaml")
    routes = [d for d in docs if d.get("kind") == "IngressRouteTCP"]
    assert {d["metadata"]["name"] for d in routes} == {"salt-publish", "salt-request"}
    for doc in routes:
        (svc,) = doc["spec"]["routes"][0]["services"]
        assert svc["name"] == "salt-master-mq"
        assert svc.get("nativeLB") is True


def test_master_quorum_survives_drain():
    (pdb,) = [
        d for d in _load("salt-master.yaml") if d.get("kind") == ("PodDisruptionBudget")
    ]
    # 3-node Raft needs two voters: a drain must never take two masters.
    assert pdb["spec"]["minAvailable"] == 2
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


def test_master_trio_replicas_and_image():
    sts = _salt_master_sts()
    assert sts["spec"]["replicas"] == 3
    containers = sts["spec"]["template"]["spec"]["containers"]
    (master,) = [c for c in containers if c["name"] == "salt-master"]
    assert master["image"].startswith("quay.io/sigaint/overstate-salt-master:")
    assert "ghcr.io" not in master["image"]


def test_master_rolls_out_sequentially():
    sts = _salt_master_sts()
    assert sts["spec"]["podManagementPolicy"] == "OrderedReady"
    assert sts["spec"]["updateStrategy"]["type"] == "RollingUpdate"


def test_master_readiness_means_joined():
    containers = _salt_master_sts()["spec"]["template"]["spec"]["containers"]
    (master,) = [c for c in containers if c["name"] == "salt-master"]
    probe = master["readinessProbe"]
    text = " ".join(probe["exec"]["command"])
    # Joined marker plus a serving check: restarts must not go Ready
    # on the previous join marker before the new daemon boots.
    assert "/home/salt/data/keys/.cluster_ready" in text
    assert "8000" in text
    assert probe["initialDelaySeconds"] >= 30


def test_master_trio_shares_keypair_but_not_accepted_keys():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["master-keypair"]["secret"]["secretName"] == "salt-master-keys"
    assert volumes["cluster-keypair"]["secret"]["secretName"] == "salt-master-cluster-keys"
    items = {
        i["key"] for i in volumes["cluster-keypair"]["secret"]["items"]
    }
    assert items == {"cluster.pem", "cluster.pub"}
    assert "keys" not in volumes  # accepted keys ride the claim template
    templates = {t["metadata"]["name"] for t in sts["spec"]["volumeClaimTemplates"]}
    assert templates == {"keys"}
    (master,) = [c for c in pod["containers"] if c["name"] == "salt-master"]
    mounts = {
        m["mountPath"]: m for m in master["volumeMounts"] if "keys" in m["mountPath"]
    }
    assert mounts["/home/salt/data/keys/master.pem"]["subPath"] == "master.pem"
    assert mounts["/home/salt/data/keys/master.pub"]["subPath"] == "master.pub"
    assert mounts["/home/salt/data/cluster-keys"]["readOnly"] is True


def test_keypair_secret_is_owner_created_not_committed():
    text = (K8S / "kustomization.yaml").read_text()
    for name in (
        "salt-master-keys.yaml",
        "salt-master-cluster-keys.yaml",
        "salt-master-cluster.yaml",
        "salt-master-db.yaml",
    ):
        assert name not in text
        assert not (K8S / name).exists()


def test_app_role_grants_no_secret_access():
    role = _app_role()
    assert "secrets" not in [
        r for rule in role["rules"] for r in rule.get("resources", [])
    ]


def _rbac_doc(kind, name):
    (doc,) = [
        d
        for d in _load("rbac.yaml")
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name
    ]
    return doc


def test_master_has_dedicated_service_account():
    sts = _salt_master_sts()
    assert sts["spec"]["template"]["spec"]["serviceAccountName"] == "salt-master"
    _rbac_doc("ServiceAccount", "salt-master")


def test_master_peers_role_is_read_only_and_namespaced():
    role = _rbac_doc("Role", "salt-master-peers")
    for rule in role["rules"]:
        assert set(rule.get("verbs", [])) <= {"get", "list"}
        assert "secrets" not in rule.get("resources", [])
        assert "configmaps" not in rule.get("resources", [])
        assert "pods" not in rule.get("resources", [])
    kinds = set()
    for rule in role["rules"]:
        kinds.update(
            (rule.get("apiGroups", [""])[0], r) for r in rule.get("resources", [])
        )
    assert ("discovery.k8s.io", "endpointslices") in kinds
    binding = _rbac_doc("RoleBinding", "salt-master-peers")
    assert binding["roleRef"]["name"] == "salt-master-peers"
    assert {(s.get("kind"), s.get("name")) for s in binding["subjects"]} == {
        ("ServiceAccount", "salt-master")
    }


def test_master_image_ships_peer_discovery_helper():
    text = (REPO / "Containerfile.salt-master").read_text(encoding="utf-8")
    assert "COPY cluster-peers.py /usr/local/bin/cluster-peers.py" in text
    assert (REPO / "cluster-peers.py").exists()
    assert "COPY cluster-ready.py /usr/local/bin/cluster-ready.py" in text
    assert (REPO / "cluster-ready.py").exists()


def test_master_image_applies_stable_identity_patch():
    # The build redirects salt's interface-keyed cluster identity to
    # cluster_node_id and must fail loudly on upstream drift.
    text = (REPO / "Containerfile.salt-master").read_text(encoding="utf-8")
    assert (
        "COPY salt-cluster-identity-patch.py /usr/local/bin/salt-cluster-identity-patch.py"
        in text
    )
    assert "salt-cluster-identity-patch.py" in text
    assert (REPO / "salt-cluster-identity-patch.py").exists()


def test_keypair_secret_readable_by_salt_user():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == 1000
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["master-keypair"]["secret"]["defaultMode"] == 0o440
    assert volumes["cluster-keypair"]["secret"]["defaultMode"] == 0o440


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
    assert "salt-master-db.yaml" not in (K8S / "kustomization.yaml").read_text()
    assert not (K8S / "salt-master-db.yaml").exists()


def test_cluster_credential_rides_a_secret_not_the_configmap():
    sts = _salt_master_sts()
    pod = sts["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    sources = volumes["config"]["projected"]["sources"]
    kinds = set()
    for source in sources:
        kind, ref = next(iter(source.items()))
        kinds.add((kind, ref.get("name")))
    assert ("secret", "salt-master-cluster") in kinds
    text = (K8S / "salt-master-config.yaml").read_text()
    assert not re.search(r"(?m)^\s*cluster_secret\s*:", text)
    assert "salt-master-cluster.yaml" not in (K8S / "kustomization.yaml").read_text()
    assert not (K8S / "salt-master-cluster.yaml").exists()


def test_master_cluster_wiring():
    docs = _load("salt-master.yaml")
    (sts,) = [d for d in docs if d.get("kind") == "StatefulSet"]
    (master,) = [
        c
        for c in sts["spec"]["template"]["spec"]["containers"]
        if c["name"] == "salt-master"
    ]
    ports = {p["name"]: p["containerPort"] for p in master["ports"]}
    assert ports["cluster"] == 4507
    (headless,) = [
        d
        for d in docs
        if d.get("kind") == "Service" and d["metadata"]["name"] == "salt-master"
    ]
    svc_ports = {p["name"]: p["port"] for p in headless["spec"]["ports"]}
    assert svc_ports["cluster"] == 4507
    text = (K8S / "salt-master-config.yaml").read_text()
    for key in (
        "cluster_id:",
        "cluster_port:",
        "cluster_pki_dir:",
        "cluster_isolated_filesystem:",
    ):
        assert key in text
    # Peers are stamped per-pod at boot (stable DNS names): a static
    # entry in the shared drop-in would beat the stamp (drop-ins win)
    # and mismatch the stamped identity, breaking the join.
    assert "cluster_peers:" not in text


def test_master_pods_carry_name_for_cluster_election():
    # Cluster identity (id/cluster_node_id/peers as the stable pod DNS
    # name, interface as the pod IP) is stamped at boot: static values
    # make every pod bootstrap a solo cluster.
    sts = _salt_master_sts()
    (master,) = [
        c
        for c in sts["spec"]["template"]["spec"]["containers"]
        if c["name"] == "salt-master"
    ]
    env = {e["name"]: e for e in master["env"]}
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["POD_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"


def test_headless_service_publishes_unready_pods():
    # Cluster boot binds `interface` to the pod's own DNS name before
    # it is Ready; without this the name never resolves and the daemon
    # can never bind (chicken-and-egg).
    docs = _load("salt-master.yaml")
    (headless,) = [
        d
        for d in docs
        if d.get("kind") == "Service" and d["metadata"]["name"] == "salt-master"
    ]
    assert headless["spec"].get("publishNotReadyAddresses") is True

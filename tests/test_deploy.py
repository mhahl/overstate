"""Deploy artifact tests: Caddyfile and Quadlet units stay consistent."""

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
K8S = REPO / "deploy" / "kubernetes"


def _load(name):
    with open(K8S / name, encoding="utf-8") as fh:
        return list(yaml.safe_load_all(fh))


def test_caddyfile_proxies_both_names_with_verified_tls():
    text = (REPO / "deploy" / "Caddyfile").read_text()
    assert "{$APP_DOMAIN:overstate.sigaint.au}" in text
    assert "{$API_DOMAIN:overstate-api.sigaint.au}" in text
    assert "reverse_proxy https://overstate-app:8000" in text
    assert "reverse_proxy https://overstate-salt-master:8000" in text
    assert "tls_trust_pool file /etc/caddy/ca.crt" in text
    assert "tls_insecure_skip_verify" not in text
    assert "admin off" in text


def test_quadlet_units_live_with_deploy_code():
    assert not (REPO / "quadlet").exists()
    units = REPO / "deploy" / "quadlet"
    assert units.is_dir()
    for script in ("install.sh", "update.sh"):
        text = (REPO / "scripts" / script).read_text()
        assert "deploy/quadlet" in text
        assert "$REPO/quadlet" not in text and '"quadlet"' not in text


def test_quadlet_units_cover_all_services():
    names = {p.name for p in (REPO / "deploy" / "quadlet").glob("*")}
    assert names == {
        "overstate.network",
        "overstate-app.container",
        "overstate-worker.container",
        "overstate-salt-master.container",
        "overstate-postgres.container",
        "overstate-redis.container",
        "overstate-caddy.container",
    }
    caddy = (REPO / "deploy" / "quadlet" / "overstate-caddy.container").read_text()
    assert "PublishPort=80:80" in caddy
    assert "PublishPort=443:443" in caddy
    assert "/etc/overstate/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy
    app = (REPO / "deploy" / "quadlet" / "overstate-app.container").read_text()
    assert "PublishPort" not in app  # Caddy is the only front door


def test_app_image_and_unit_support_ssh_push():
    containerfile = (REPO / "Containerfile").read_text()
    assert "openssh-client" in containerfile  # push over SSH deploy keys
    app = (REPO / "deploy" / "quadlet" / "overstate-app.container").read_text()
    assert "/etc/overstate/ssh:/srv/ssh:ro" in app  # key dir, read-only


def test_salt_master_publishes_minion_ports():
    lines = (
        (REPO / "deploy" / "quadlet" / "overstate-salt-master.container")
        .read_text()
        .splitlines()
    )
    published = [line for line in lines if line.startswith("PublishPort=")]
    assert "PublishPort=4505:4505" in published
    assert "PublishPort=4506:4506" in published
    # salt-api stays localhost-only behind Caddy; only the minion ports
    # are reachable from the network.
    assert [line for line in published if ":8000" in line] == [
        "PublishPort=127.0.0.1:8001:8000"
    ]


def test_prod_scripts_strip_dev_auto_accept():
    # install.sh copies salt-config/ wholesale, which includes the dev-only
    # auto_accept file; both scripts must remove it from the deployed copy
    # so a real master never auto-accepts minion keys.
    for script in ("install.sh", "update.sh"):
        text = (REPO / "scripts" / script).read_text()
        assert 'rm -f "$ETC/salt-config/dev.conf"' in text


def test_auto_accept_lives_only_in_dev_only_file():
    confs = sorted((REPO / "salt-config").glob("*.conf"))
    assert len(confs) >= 2
    hits = [path.name for path in confs if "auto_accept" in path.read_text()]
    assert hits == ["dev.conf"]


def test_quadlet_names_cover_every_dialed_host():
    import re

    units = REPO / "deploy" / "quadlet"
    names = set()
    for unit in units.glob("*.container"):
        found = re.findall(r"^ContainerName=(.+)$", unit.read_text(), re.MULTILINE)
        assert len(found) == 1, unit.name
        names.add(found[0].strip())
    assert len(names) == len(list(units.glob("*.container")))
    dialed = {"postgres", "redis", "salt-master", "overstate-app"}
    assert dialed <= names, dialed - names


def test_every_mount_carries_selinux_relabel():
    import re

    for unit in (REPO / "deploy" / "quadlet").glob("*.container"):
        for line in unit.read_text().splitlines():
            if line.startswith("Volume="):
                assert re.search(r"[:,][zZ](,|$)", line), f"{unit.name}: {line}"


def test_api_tls_install_wired_into_flows():
    helper = REPO / "scripts" / "install-api-tls.sh"
    assert helper.is_file()
    text = helper.read_text()
    assert "supervisorctl restart salt-api" in text
    assert "localhost.crt" in text
    unit = REPO / "deploy" / "systemd" / "overstate-salt-api-tls.service"
    assert unit.is_file()
    unit_text = unit.read_text()
    assert "WantedBy=overstate-salt-master.service" in unit_text
    assert "BindsTo=overstate-salt-master.service" in unit_text
    assert "overstate-install-api-tls.sh" in unit_text
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "deploy/systemd/overstate-salt-api-tls.service" in install
    assert "overstate-salt-api-tls.service" in install
    update = (REPO / "scripts" / "update.sh").read_text()
    assert "overstate-salt-api-tls.service" in update
    uninstall = (REPO / "scripts" / "uninstall.sh").read_text()
    assert "overstate-salt-api-tls" in uninstall


def test_install_tolerates_generator_wiring():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "enable_unit()" in install
    assert "is-enabled" in install
    assert "systemctl enable --now overstate-" not in install


def test_install_syncs_returner_password():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "returner.pgjsonb.pass" in install
    assert "POSTGRES_PASSWORD" in install
    assert "restart overstate-salt-master" in install


def test_api_key_readable_by_salt_user():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert 'chmod 644 "$ETC/tls/api.key"' in install


def test_app_cert_covers_internal_dial_name():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "DNS:overstate-app" in install


def test_install_targets_leap_16():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "opensuse-leap" in install
    assert "openSUSE Leap 16" in install
    assert "Tumbleweed" not in install or "non-Leap-16" in install
    guide = (REPO / "docs" / "install-opensuse.md").read_text()
    assert guide.startswith("# Install on openSUSE Leap 16")


def test_caddy_skips_gzip_for_event_streams():
    text = (REPO / "deploy" / "Caddyfile").read_text()
    assert "text/event-stream" in text
    assert "encode @notsse gzip" in text
    assert "not header Accept text/event-stream" in text


def test_install_wires_domains_and_caddy():
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "APP_DOMAIN=overstate.sigaint.au" in install
    assert "API_DOMAIN=overstate-api.sigaint.au" in install
    assert "overstate-caddy.service" in install
    assert "deploy/Caddyfile" in install
    update = (REPO / "scripts" / "update.sh").read_text()
    assert "overstate-caddy.service" in update
    uninstall = (REPO / "scripts" / "uninstall.sh").read_text()
    assert "overstate-caddy" in uninstall


def test_redis_requires_a_password():
    compose = (REPO / "compose.yml").read_text()
    assert "--requirepass" in compose
    assert "REDIS_PASSWORD" in compose
    assert "redis://redis:6379/0" not in compose
    redis_unit = (REPO / "deploy" / "quadlet" / "overstate-redis.container").read_text()
    assert "--requirepass" in redis_unit
    install = (REPO / "scripts" / "install.sh").read_text()
    assert "REDIS_PASSWORD" in install
    assert "REDIS_URL=redis://:" in install
    example = (REPO / ".env.example").read_text()
    assert "REDIS_PASSWORD" in example


def test_kustomization_wires_history_configmap():
    text = (K8S / "kustomization.yaml").read_text()
    assert "salt-master-config-history.yaml" in text


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

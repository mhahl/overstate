"""Deploy artifact tests: Caddyfile and Quadlet units stay consistent."""

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_caddyfile_proxies_both_names_with_verified_tls():
    text = (REPO / "deploy" / "Caddyfile").read_text()
    assert "{$APP_DOMAIN:overstate.sigaint.au}" in text
    assert "{$API_DOMAIN:overstate-api.sigaint.au}" in text
    assert "reverse_proxy https://overstate-app:8000" in text
    assert "reverse_proxy https://overstate-salt-master:8000" in text
    assert "tls_trust_pool file /etc/caddy/ca.crt" in text
    assert "tls_insecure_skip_verify" not in text
    assert "admin off" in text


def test_quadlet_units_cover_all_services():
    names = {p.name for p in (REPO / "quadlet").glob("*")}
    assert names == {
        "overstate.network",
        "overstate-app.container",
        "overstate-worker.container",
        "overstate-salt-master.container",
        "overstate-postgres.container",
        "overstate-redis.container",
        "overstate-caddy.container",
    }
    caddy = (REPO / "quadlet" / "overstate-caddy.container").read_text()
    assert "PublishPort=80:80" in caddy
    assert "PublishPort=443:443" in caddy
    assert "/etc/overstate/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy
    app = (REPO / "quadlet" / "overstate-app.container").read_text()
    assert "PublishPort" not in app  # Caddy is the only front door


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

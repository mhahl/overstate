"""Proxy regression: login behind Caddy must pass CSRF origin checks.

Caddy dials the app at overstate-app:8000 while the browser sees
https://<public-host> with no port. Flask-WTF compares the Referer
origin against request.host byte-exactly (scheme, hostname, port),
so without ProxyFix + forwarded host/port every login POST 400s with
"The referrer does not match the host."
"""

import re

from overstate_ui import create_app
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db


class ProxyConfig(TestConfig):
    WTF_CSRF_ENABLED = True


def _client(monkeypatch=None, trust_proxy=True):
    # ProxyFix applies only with TRUST_PROXY=1 (production behind Caddy).
    if monkeypatch is not None:
        if trust_proxy:
            monkeypatch.setenv("TRUST_PROXY", "1")
        else:
            monkeypatch.delenv("TRUST_PROXY", raising=False)
    init_db("sqlite://")
    app = create_app(ProxyConfig)
    app.config["WTF_CSRF_ENABLED"] = True
    with app.app_context():
        create_all()
    return app.test_client()


def _token(client, base_url):
    html = client.get("/login", base_url=base_url).data.decode()
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "login page carries a CSRF token"
    return match.group(1)


def _post_login(client, base_url, extra_headers):
    token = _token(client, base_url)
    headers = {"Referer": "https://overstate.sigaint.au/login", **extra_headers}
    return client.post(
        "/login",
        base_url=base_url,
        headers=headers,
        data={"username": "nobody", "password": "wrong", "csrf_token": token},
    )


def test_login_behind_proxy_passes_referrer_check(monkeypatch):
    client = _client(monkeypatch, trust_proxy=True)
    rv = _post_login(
        client,
        "https://overstate-app:8000",
        {
            "X-Forwarded-Host": "overstate.sigaint.au",
            "X-Forwarded-Port": "443",
            "X-Forwarded-Proto": "https",
        },
    )
    assert rv.status_code != 400
    assert b"does not match the host" not in rv.data


def test_login_without_forwarded_headers_still_fails(monkeypatch):
    client = _client(monkeypatch, trust_proxy=True)
    rv = _post_login(client, "https://overstate-app:8000", {})
    assert rv.status_code == 400
    assert b"does not match the host" in rv.data


def test_spoofed_forwarded_host_ignored_without_trust_proxy(monkeypatch):
    # Direct gunicorn access must not let X-Forwarded-Host rewrite
    # _external URLs (e.g. the OIDC redirect).
    client = _client(monkeypatch, trust_proxy=False)
    with client.application.test_request_context(
        "/",
        base_url="http://overstate-app:8000",
        headers={"X-Forwarded-Host": "evil.example"},
    ):
        from flask import url_for

        assert url_for("auth.login", _external=True).startswith(
            "http://overstate-app:8000"
        )

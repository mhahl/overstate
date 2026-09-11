"""v2 unit 5 tests: TLS verify configuration and client plumbing."""

import httpx

from overstate_ui.config import _verify_setting
from overstate_ui.salt_client import SaltClient


def test_verify_setting_defaults_to_system_cas():
    assert _verify_setting("") is True


def test_verify_setting_false_only_on_literal():
    assert _verify_setting("false") is False
    assert _verify_setting("FALSE") is False
    assert _verify_setting("/srv/tls/ca.crt") == "/srv/tls/ca.crt"


def test_client_verifies_by_default(monkeypatch):
    seen = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    SaltClient("https://salt:8000", "u", "p")
    assert seen.get("verify") is True


def test_client_accepts_ca_bundle(monkeypatch):
    seen = {}

    class Dummy:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(httpx, "Client", Dummy)
    client = SaltClient("https://salt:8000", "u", "p", verify="/srv/tls/ca.crt")
    assert seen.get("verify") == "/srv/tls/ca.crt"
    assert client.verify == "/srv/tls/ca.crt"

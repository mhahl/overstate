"""salt-cli `salt` wrapper tests: 401 vs unreachable must not conflate.

The wrapper fans one CLI call out to every master + the API service.
It used to print "could not contact salt-master cluster" whenever no
endpoint yielded data — including the case where every endpoint was
reached fine but refused the function with HTTP 401 (unknown function
like highstate.apply, or missing eauth grant). These tests pin the
distinct messages through a stubbed urlopen.
"""

import importlib.machinery
import importlib.util
import json
import pathlib
import urllib.error

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
WRAPPER = REPO / "salt-cli-wrappers" / "salt"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _load(monkeypatch):
    monkeypatch.setenv("OVERSTATE_ROLE", "operator")
    monkeypatch.setenv("SALT_API_USER", "overstate")
    monkeypatch.setenv("SALT_API_USER_PASS", "pw")
    # Extensionless wrapper: give importlib an explicit source loader.
    loader = importlib.machinery.SourceFileLoader("salt_cli_wrapper", str(WRAPPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run(monkeypatch, capsys, module, behaviour):
    """behaviour(is_login) -> 'ok' | 'denied' | 'login-denied' | 'down'.

    'denied' is only meaningful on the call phase (login already
    succeeded); 'login-denied' only on the login phase. Tests pass
    phase-appropriate values, mirroring real salt-api behavior.
    """

    def fake_urlopen(req, context=None, timeout=None):
        url = req.full_url
        is_login = url.endswith("/login")
        outcome = behaviour(is_login)
        if outcome == "down":
            raise urllib.error.URLError("refused")
        if outcome == "denied" and not is_login:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if outcome == "login-denied" and is_login:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if is_login:
            return FakeResponse({"return": [{"token": "tok"}]})
        return FakeResponse({"return": [{"minion1": True}]})

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["salt", "*", "highstate.apply"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    return exc.value.code, capsys.readouterr().err


def test_all_down_keeps_cluster_message(monkeypatch, capsys):
    module = _load(monkeypatch)
    code, err = _run(monkeypatch, capsys, module, lambda login: "down")
    assert code == 1
    assert "could not contact salt-master cluster" in err


def test_all_denied_reports_401_not_cluster(monkeypatch, capsys):
    module = _load(monkeypatch)
    # Login succeeds everywhere (credentials are fine); every call 401s.
    code, err = _run(
        monkeypatch, capsys, module, lambda login: "ok" if login else "denied"
    )
    assert code == 1
    assert "could not contact" not in err
    assert "refused (HTTP 401)" in err
    assert "state.highstate" in err


def test_all_login_denied_reports_credentials(monkeypatch, capsys):
    module = _load(monkeypatch)
    code, err = _run(
        monkeypatch, capsys, module, lambda login: "login-denied" if login else "down"
    )
    assert code == 1
    assert "login rejected (HTTP 401)" in err
    assert "SALT_API_USER_PASS" in err


def test_mixed_denied_and_down_lists_unreachable(monkeypatch, capsys):
    module = _load(monkeypatch)
    seen = {"n": 0}

    def behaviour(login):
        # First endpoint (login+call) denies; everything else is down.
        if not login and seen["n"] == 0:
            seen["n"] += 1
            return "denied"
        if login and seen["n"] == 0:
            return "ok"
        return "down"

    code, err = _run(monkeypatch, capsys, module, behaviour)
    assert code == 1
    assert "refused (HTTP 401)" in err
    assert "Unreachable endpoints" in err


def test_success_still_merges_and_prints(monkeypatch, capsys):
    module = _load(monkeypatch)

    def fake_urlopen(req, context=None, timeout=None):
        if req.full_url.endswith("/login"):
            return FakeResponse({"return": [{"token": "tok"}]})
        return FakeResponse({"return": [{"minion1": True}]})

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["salt", "*", "test.ping"])
    module.main()  # Must not raise SystemExit.
    out = capsys.readouterr().out
    assert "minion1:" in out

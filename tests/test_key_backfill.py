"""Accepted-key backfill: a first check-in lands pending on one pod only.

Wheel ``key.accept`` only moves keys already filed on that pod, so the
fan-out accept no-ops everywhere else and the fleet stays split until
the minion roams. The backfill copies the byte-identical accepted
pubkey onto reachable pods missing it (fingerprint-unanimous sources
only) and verifies hash plus re-read state per pod.
"""

import hashlib
import re
import shlex
from types import SimpleNamespace

import pytest

import overstate_ui.keys as keys_mod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, init_db
from overstate_ui.k8s import K8sError

PEM_A = "-----BEGIN PUBLIC KEY-----\nAAAABBBBCCCCweb1\n-----END PUBLIC KEY-----\n"
PEM_B = "-----BEGIN PUBLIC KEY-----\nZZZZYYYYXXXXweb1\n-----END PUBLIC KEY-----\n"
FP_A = "aa:" * 19 + "aa"
FP_B = "bb:" * 19 + "bb"


class FakeSaltClient:
    """Stateful salt-api pod stub: key dirs, Salt-shaped wheel returns."""

    def __init__(
        self,
        *,
        accepted=(),
        pending=(),
        rejected=(),
        denied=(),
        pubkeys=None,
        fps=None,
    ):
        self.accepted = set(accepted)
        self.pending = set(pending)
        self.rejected = set(rejected)
        self.denied = set(denied)
        self.pubkeys = dict(pubkeys or {})
        self.fps = dict(fps or {})
        self.seen_accept = []

    def _listing(self):
        return {
            "minions": sorted(self.accepted),
            "minions_pre": sorted(self.pending),
            "minions_rejected": sorted(self.rejected),
            "minions_denied": sorted(self.denied),
        }

    def wheel(self, fun, **kwargs):
        if fun == "key.list_all":
            return [{"data": {"return": self._listing()}}]
        if fun == "key.finger":
            sections = {}
            for section, ids in (
                ("minions", self.accepted),
                ("minions_pre", self.pending),
                ("minions_rejected", self.rejected),
                ("minions_denied", self.denied),
            ):
                have = {mid: self.fps[mid] for mid in ids if mid in self.fps}
                if have:
                    sections[section] = have
            return [{"data": {"return": sections}}]
        if fun == "key.accept":
            mid = kwargs.get("match")
            self.seen_accept.append(mid)
            if mid in self.pending:
                self.pending.discard(mid)
                self.accepted.add(mid)
            elif mid in self.rejected and kwargs.get("include_rejected"):
                self.rejected.discard(mid)
                self.accepted.add(mid)
            elif mid in self.denied and kwargs.get("include_denied"):
                self.denied.discard(mid)
                self.accepted.add(mid)
            return [{"data": {"return": self._listing(), "success": True}}]
        if fun == "key.print":
            # salt/wheel/key.py aliases key_str -> print; key.key_str is
            # not callable over salt-api (the loader registers only the
            # aliased name), so this mock must not accept the old name.
            mid = kwargs.get("match")
            sections = {}
            if mid in self.accepted and mid in self.pubkeys:
                sections = {"minions": {mid: self.pubkeys[mid]}}
            return [{"data": {"return": sections}}]
        raise AssertionError(f"unexpected wheel call {fun!r}")


def _unquote(word):
    return shlex.split(f"X={word}")[0][2:]


class FakeK8s:
    """Emulates the backfill exec: writes the quoted PEM into the pod store."""

    def __init__(self, stores, *, fail=(), bad_digest=()):
        self.config = SimpleNamespace(available=True)
        self.calls = []
        self._stores = stores
        self._fail = set(fail)
        self._bad_digest = set(bad_digest)

    def exec_command(self, pod, command, timeout=60.0):
        self.calls.append((pod, command))
        if pod in self._fail:
            raise K8sError("exec stream refused")
        script = command[2]
        pem = _unquote(
            re.search(r"printf '%s' (.*?) > \"\$tmp\";", script, re.DOTALL).group(1)
        )
        dest = _unquote(re.search(r"dest=([^;]+);", script).group(1))
        mid = dest.rsplit("/", 1)[-1]
        store = self._stores[pod]
        store.accepted.add(mid)
        store.pubkeys[mid] = pem
        if pod in self._bad_digest:
            return {"ok": True, "exit_code": 0, "stdout": "deadbeef\n", "stderr": ""}
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": hashlib.sha256(pem.encode()).hexdigest() + "\n",
            "stderr": "",
        }


def _clients_pair(**kwargs):
    a = FakeSaltClient(**kwargs.get("a", {}))
    b = FakeSaltClient(**kwargs.get("b", {}))
    return [("pod-0", a), ("pod-1", b)], a, b


MAPPING = {"pod-0": "salt-master-0", "pod-1": "salt-master-1"}


def _stores(a, b):
    return {"salt-master-0": a, "salt-master-1": b}


def _first_checkin():
    return _clients_pair(
        a={"pending": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )


def test_backfill_first_checkin_converges(monkeypatch):
    clients, a, b = _first_checkin()
    # Simulate the fan-out accept first: pod-0 moves pending -> accepted.
    a.wheel("key.accept", match="web1", include_rejected=True, include_denied=True)
    fake = FakeK8s(_stores(a, b))
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("web1", clients, k8s_pods=MAPPING)
    assert report["backfilled"] == ["pod-1"]
    assert report["skipped"] == [] and report["errors"] == []
    assert report["unavailable"] is False
    assert "web1" in b.accepted and b.pubkeys["web1"] == PEM_A
    assert len(fake.calls) == 1 and fake.calls[0][0] == "salt-master-1"


def test_backfill_converged_is_noop(monkeypatch):
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
    )
    fake = FakeK8s(_stores(a, b))
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("web1", clients, k8s_pods=MAPPING)
    assert report["backfilled"] == [] and report["errors"] == []
    assert fake.calls == []


def test_backfill_needs_unanimous_fingerprint(monkeypatch):
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={"accepted": ["web1"], "pubkeys": {"web1": PEM_B}, "fps": {"web1": FP_B}},
    )
    # pod-2 never saw the key: absent while the accepted copies disagree.
    c = FakeSaltClient()
    clients.append(("pod-2", c))
    mapping = dict(MAPPING, **{"pod-2": "salt-master-2"})
    fake = FakeK8s({"salt-master-0": a, "salt-master-1": b, "salt-master-2": c})
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("web1", clients, k8s_pods=mapping)
    assert report["backfilled"] == []
    assert any("disagree" in reason for _, reason in report["skipped"])
    assert fake.calls == [] and "web1" not in c.accepted


def test_backfill_never_overrides_quarantine(monkeypatch):
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={"rejected": ["web1"], "fps": {"web1": FP_A}},
    )
    fake = FakeK8s(_stores(a, b))
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_absent_keys(clients, k8s_pods=MAPPING)
    assert report["backfilled"] == [] and report["plan"] == []
    assert fake.calls == []


def test_backfill_leaves_globally_pending_alone(monkeypatch):
    clients, a, b = _clients_pair(
        a={"pending": ["new1"], "fps": {"new1": FP_A}},
        b={"pending": ["new1"], "fps": {"new1": FP_A}},
    )
    fake = FakeK8s(_stores(a, b))
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_absent_keys(clients, k8s_pods=MAPPING)
    assert report["backfilled"] == [] and report["plan"] == []
    assert fake.calls == []


def test_backfill_unavailable_without_k8s():
    clients, _a, _b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    # No app context, no stubbed K8s: mapping cannot resolve.
    report = keys_mod.backfill_key("web1", clients)
    assert report["unavailable"] is True
    assert report["backfilled"] == []


def test_backfill_digest_mismatch_errors(monkeypatch):
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    fake = FakeK8s(_stores(a, b), bad_digest={"salt-master-1"})
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("web1", clients, k8s_pods=MAPPING)
    assert report["backfilled"] == []
    assert len(report["errors"]) == 1 and report["errors"][0][0] == "pod-1"


def test_backfill_exec_failure_errors(monkeypatch):
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    fake = FakeK8s(_stores(a, b), fail={"salt-master-1"})
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("web1", clients, k8s_pods=MAPPING)
    assert report["backfilled"] == []
    assert len(report["errors"]) == 1


def test_backfill_rejects_unsafe_id(monkeypatch):
    clients, a, b = _clients_pair(
        a={
            "accepted": ["../evil"],
            "pubkeys": {"../evil": PEM_A},
            "fps": {"../evil": FP_A},
        },
        b={},
    )
    fake = FakeK8s(_stores(a, b))
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    report = keys_mod.backfill_key("../evil", clients, k8s_pods=MAPPING)
    assert report["backfilled"] == []
    assert any("unsafe" in reason for _, reason in report["skipped"])
    assert fake.calls == []


def test_backfill_script_is_atomic_and_quoted(tmp_path):
    pem = "line1\nline2'with-quote\n-----END PUBLIC KEY-----\n"
    mid = "web id'x"
    dest = str(tmp_path / "minions" / mid)
    script = keys_mod._backfill_script(pem, dest)[2]
    assert script.startswith("set -eu;")
    assert shlex.quote(pem) in script
    assert f"dest={shlex.quote(dest)};" in script
    assert "mktemp" in script and "mv -f" in script and "sha256sum" in script


def test_read_pubkey_uses_callable_key_print_alias():
    """salt/wheel/key.py aliases key_str -> print (__func_alias__), so the
    backfill must call ``key.print`` over salt-api. A client that only
    answers ``key.print`` (and would error out of the blue on anything
    else) proves the code no longer calls the non-callable ``key.key_str``.
    """
    PEM = PEM_A
    calls = []

    class AliasClient:
        def wheel(self, fun, **kwargs):
            calls.append(fun)
            assert fun == "key.print"  # key.key_str must never be issued
            assert kwargs["match"] == "web1"
            return [{"data": {"return": {"minions": {"web1": PEM}}}}]

    pem = keys_mod._read_accepted_pubkey({"pod-0": AliasClient()}, "pod-0", "web1")
    assert pem == PEM
    assert calls == ["key.print"]


def test_read_pubkey_fails_closed_on_error_return():
    """A salt-api ``success:false`` (unknown wheel fun / auth loss) returns
    an error string or empty data in ``return``; the backfill must treat
    that as unreadable, not fabricate a key."""
    class ErrorClient:
        def wheel(self, fun, **kwargs):
            return [{"data": {"return": "Key not found: key.print"}, "success": False}]

    pem = keys_mod._read_accepted_pubkey({"pod-0": ErrorClient()}, "pod-0", "web1")
    assert pem is None


@pytest.fixture()
def app_client(monkeypatch):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "pw"})
    return app, client, monkeypatch


def _sts_names(app):
    sts = app.config["MASTER_STATEFULSET"]
    return {"pod-0": f"{sts}-0", "pod-1": f"{sts}-1"}


def test_accept_route_warns_when_backfill_unavailable(app_client, monkeypatch):
    # Same split, no K8s: the accept must not claim full convergence.
    _app, client, _mp = app_client
    clients, _a, _b = _first_checkin()
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: clients)
    rv = client.post(
        "/keys/accept", data={"id": "web1", "tab": "pending"}, follow_redirects=True
    )
    assert rv.status_code == 200
    assert b"not copied to pod-1" in rv.data
    assert b"Backfilled to" not in rv.data


def test_accept_route_backfills_absent_pod(app_client, monkeypatch):
    app, client, _mp = app_client
    clients, a, b = _first_checkin()
    fake = FakeK8s(
        {
            f"{app.config['MASTER_STATEFULSET']}-0": a,
            f"{app.config['MASTER_STATEFULSET']}-1": b,
        }
    )
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: clients)
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    rv = client.post(
        "/keys/accept", data={"id": "web1", "tab": "pending"}, follow_redirects=True
    )
    assert rv.status_code == 200
    assert b"web1: accepted." in rv.data
    assert b"Backfilled to pod-1." in rv.data
    assert "web1" in a.accepted and "web1" in b.accepted
    assert b.pubkeys["web1"] == PEM_A


def test_reconcile_preview_lists_backfill_plan(app_client, monkeypatch):
    _app, client, _mp = app_client
    clients, _a, _b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: clients)
    rv = client.post("/keys/reconcile")
    assert rv.status_code == 200
    assert b"Keys to copy" in rv.data and b"pod-1" in rv.data


def test_reconcile_confirm_backfills(app_client, monkeypatch):
    app, client, _mp = app_client
    clients, a, b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    fake = FakeK8s(
        {
            f"{app.config['MASTER_STATEFULSET']}-0": a,
            f"{app.config['MASTER_STATEFULSET']}-1": b,
        }
    )
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: clients)
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    rv = client.post("/keys/reconcile", data={"confirm": "1"}, follow_redirects=True)
    assert rv.status_code == 200
    assert b"Backfilled 1 key(s)" in rv.data
    assert "web1" in b.accepted


def test_index_missing_banner(app_client, monkeypatch):
    _app, client, _mp = app_client
    clients, _a, _b = _clients_pair(
        a={"accepted": ["web1"], "pubkeys": {"web1": PEM_A}, "fps": {"web1": FP_A}},
        b={},
    )
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: clients)
    rv = client.get("/keys/?tab=accepted")
    assert rv.status_code == 200
    assert b"1 accepted key(s) are missing" in rv.data


def test_console_accept_backfills_absent_pod(app_client, monkeypatch):
    import overstate_ui.console as console_mod

    app, client, _mp = app_client
    clients, a, b = _first_checkin()
    fake = FakeK8s(
        {
            f"{app.config['MASTER_STATEFULSET']}-0": a,
            f"{app.config['MASTER_STATEFULSET']}-1": b,
        }
    )
    monkeypatch.setattr(console_mod, "pod_clients", lambda default: clients)
    monkeypatch.setattr(keys_mod, "K8sClient", lambda: fake)
    rv = client.post("/console/run", json={"line": "salt-key -a web1"})
    assert rv.status_code == 200
    assert "Backfilled to pod-1" in rv.get_json()["output"]
    assert "web1" in a.accepted and "web1" in b.accepted

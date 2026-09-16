"""Key reconcile: complete trust across the pair, never create it.

A minion pending on one pod but accepted on another (same fingerprint)
is finished scale-up / outage drift: safe to accept. Everything else —
globally pending, fingerprint mismatch, reject/deny anywhere, missing
fingerprints — stays for a human.
"""

import json

import httpx
import pytest

import overstate_ui.keys as keys_mod
from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import AuditEvent
from overstate_ui.salt_client import SaltClient

FP1 = "aa:" * 19 + "aa"
FP2 = "bb:" * 19 + "bb"


def _listing(**tabs):
    base = {
        "minions": [],
        "minions_pre": [],
        "minions_rejected": [],
        "minions_denied": [],
    }
    base.update(tabs)
    return base


def _finger_sections(**sections):
    return sections


def _pod(seen, *, listing, fingers, down=False, fail_accept=False):
    """Stub salt-api pod. seen records every key.accept match it gets."""

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("pod gone")
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        fun = body.get("fun")
        if fun == "key.list_all":
            payload = {"return": [{"data": {"return": listing}}]}
        elif fun == "key.finger":
            payload = {"return": [{"data": {"return": fingers}}]}
        elif fun == "key.accept":
            if fail_accept:
                return httpx.Response(500, json={"return": []})
            seen.append(body.get("match"))
            payload = {"return": [{"data": {"return": True}}]}
        else:  # pragma: no cover - reject/delete never fire here
            raise AssertionError(f"unexpected wheel call {fun!r}")
        return httpx.Response(200, json=payload)

    return SaltClient(
        "https://pod:8000", "u", "p", transport=httpx.MockTransport(handler)
    )


def _clients(seen_a, seen_b, **kwargs):
    return [
        ("pod-0", _pod(seen_a, **kwargs["a"])),
        ("pod-1", _pod(seen_b, **kwargs["b"])),
    ]


def test_completes_pending_where_trusted_same_fingerprint():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={
            "listing": _listing(minions=["web1"]),
            "fingers": _finger_sections(minions={"web1": FP1}),
        },
        b={
            "listing": _listing(minions_pre=["web1"]),
            "fingers": _finger_sections(minions_pre={"web1": FP1}),
        },
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["accepted"] == [("pod-1", "web1")]
    assert seen_a == [] and seen_b == ["web1"]
    assert report["skipped"] == [] and report["unreachable"] == []


def test_skips_fingerprint_mismatch():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={
            "listing": _listing(minions=["web1"]),
            "fingers": _finger_sections(minions={"web1": FP1}),
        },
        b={
            "listing": _listing(minions_pre=["web1"]),
            "fingers": _finger_sections(minions_pre={"web1": FP2}),
        },
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["accepted"] == []
    assert seen_a == seen_b == []
    ((mid, reason),) = report["skipped"]
    assert mid == "web1" and "ingerprint" in reason


def test_leaves_globally_pending_alone():
    seen_a, seen_b = [], []
    both = {
        "listing": _listing(minions_pre=["new1"]),
        "fingers": _finger_sections(minions_pre={"new1": FP1}),
    }
    report = keys_mod.reconcile_keys(_clients(seen_a, seen_b, a=both, b=both))
    assert report["accepted"] == []
    assert seen_a == seen_b == []
    ((mid, reason),) = report["skipped"]
    assert mid == "new1" and "nowhere" in reason


def test_never_overrides_reject():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={
            "listing": _listing(minions=["web1"]),
            "fingers": _finger_sections(minions={"web1": FP1}),
        },
        b={
            "listing": _listing(minions_rejected=["web1"]),
            "fingers": _finger_sections(minions_rejected={"web1": FP1}),
        },
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["accepted"] == []
    assert seen_a == seen_b == []


def test_unreachable_pod_degrades_without_blocking():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={
            "listing": _listing(minions=["web1"]),
            "fingers": _finger_sections(minions={"web1": FP1}),
        },
        b={"listing": _listing(), "fingers": {}, "down": True},
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["unreachable"] == ["pod-1"]
    assert report["accepted"] == []  # nobody pending on a reachable pod


def test_missing_fingerprints_fail_closed():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={"listing": _listing(minions=["web1"]), "fingers": {}},
        b={"listing": _listing(minions_pre=["web1"]), "fingers": {}},
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["accepted"] == []
    assert seen_a == seen_b == []


def test_absent_elsewhere_is_noop():
    seen_a, seen_b = [], []
    clients = _clients(
        seen_a,
        seen_b,
        a={
            "listing": _listing(minions=["web1"]),
            "fingers": _finger_sections(minions={"web1": FP1}),
        },
        b={"listing": _listing(), "fingers": {}},
    )
    report = keys_mod.reconcile_keys(clients)
    assert report["accepted"] == [] and report["skipped"] == []


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


def _stub_pair(monkeypatch, seen):
    a = {
        "listing": _listing(minions=["web1"]),
        "fingers": _finger_sections(minions={"web1": FP1}),
    }
    b = {
        "listing": _listing(minions_pre=["web1"]),
        "fingers": _finger_sections(minions_pre={"web1": FP1}),
    }
    pods = [("pod-0", _pod([], **a)), ("pod-1", _pod(seen, **b))]
    monkeypatch.setattr(keys_mod, "pod_clients", lambda default: pods)


def test_route_preview_then_confirm(app_client):
    app, client, monkeypatch = app_client
    seen = []
    _stub_pair(monkeypatch, seen)
    preview = client.post("/keys/reconcile")
    assert preview.status_code == 200
    assert b"web1" in preview.data and b"pod-1" in preview.data
    assert seen == []  # preview writes nothing
    done = client.post("/keys/reconcile", data={"confirm": "1"})
    assert done.status_code == 302
    assert seen == ["web1"]
    with app.app_context():
        session = get_session()
        actions = [e.action for e in session.query(AuditEvent).all()]
    assert any(a.startswith("reconcile-keys") for a in actions)


def test_index_banner_counts_completable(app_client):
    _app, client, monkeypatch = app_client
    _stub_pair(monkeypatch, [])
    resp = client.get("/keys/")
    assert resp.status_code == 200
    assert b"scale-up drift" in resp.data

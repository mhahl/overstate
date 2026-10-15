"""Vulnerabilities tests: global CVE table and minion vuln tab.

Both surfaces read the grype mine keys through salt-api only: the
global page fans out ``grype.scan_summary`` (slim: counts/total/top),
the minion tab reads ``grype.scan_detail`` (full ``detailed_report``)
with fallback to the old combined summary format.
"""

import json

import httpx

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.models import Minion
from overstate_ui.salt_client import SaltClient


def _summary(mid, total, critical=0, high=0, fixable=0):
    return {
        "severity_filter": "HIGH,CRITICAL",
        "counts": {"HIGH": high, "CRITICAL": critical},
        "total": total,
        "fixable": fixable,
        "top": [
            {
                "cve": f"CVE-2026-{i:04d}",
                "pkg": "kernel",
                "installed": "1.0",
                "fixed": "1.1",
                "severity": "CRITICAL" if i < critical else "HIGH",
            }
            for i in range(min(total, 20))
        ],
        "distro": {"name": "Fedora", "version": "42"},
        "minion": mid,
        "scanned_at": "2026-10-15T00:00:00+00:00",
        "report_path": "/var/cache/grype/report.json",
        "grype_version": "0.1.0",
        "db_built_at": "2026-10-14T00:00:00+00:00",
    }


def _detail(mid, n=3):
    payload = _summary(mid, n, critical=1, high=n - 1, fixable=1)
    payload["detailed_report"] = [
        {
            "cve": f"CVE-2026-{i:04d}",
            "pkg": "kernel",
            "installed": "1.0",
            "fixed": "1.1" if i == 0 else "",
            "severity": "CRITICAL" if i == 0 else "HIGH",
            "namespace": "fedora:42",
            "urls": [f"https://example.test/CVE-2026-{i:04d}"],
        }
        for i in range(n)
    ]
    return payload


SUMMARIES = {
    "web-01": _summary("web-01", 3, critical=1, high=2, fixable=2),
    "web-02": _summary("web-02", 10, critical=4, high=6, fixable=5),
}
DETAILS = {"web-01": _detail("web-01")}


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200, json={"return": [{"token": "tok", "expire": 99}]}
            )
        body = json.loads(request.content or b"{}")
        if body.get("client") == "runner":
            return httpx.Response(
                200, json={"return": [{"up": ["web-01", "web-02"], "down": []}]}
            )
        if body.get("client") == "local" and body.get("fun") == "mine.get":
            tgt = body.get("tgt")
            fun = (body.get("arg") or ["*", ""])[1]
            if fun == "grype.scan_summary":
                # Salt nests mine results under the executing minion;
                # fleet lookups return every minion, single lookups one.
                data = dict(SUMMARIES)
                return httpx.Response(200, json={"return": [{tgt: data}]})
            if fun == "grype.scan_detail":
                value = DETAILS.get(tgt, {})
                return httpx.Response(200, json={"return": [{tgt: {tgt: value}}]})
            return httpx.Response(200, json={"return": [{}]})
        return httpx.Response(200, json={"return": [{}]})

    return httpx.MockTransport(handler)


def make_client():
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["salt_client"] = SaltClient(
        "https://salt:8000", "u", "p", transport=fake_transport()
    )
    with app.app_context():
        create_all()
        seed_admin(password="pw")
        session = get_session()
        session.add(Minion(id="web-01", key_status="accepted", grains={}))
        session.add(Minion(id="web-02", key_status="accepted", grains={}))
        session.commit()
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def test_global_page_lists_summaries_with_links():
    html = make_client().get("/vulnerabilities/").data.decode()
    assert "web-01" in html and "web-02" in html
    assert "/minions/web-01?tab=vulns" in html
    assert "CRITICAL" in html


def test_global_search_filters_by_minion():
    html = (
        make_client()
        .get("/vulnerabilities/", query_string={"q": "web-02"})
        .data.decode()
    )
    assert "web-02" in html
    assert "web-01" not in html


def test_global_sort_orders_whole_fleet_not_just_page():
    html = (
        make_client()
        .get("/vulnerabilities/", query_string={"sort": "total", "dir": "desc"})
        .data.decode()
    )
    assert html.index("web-02") < html.index("web-01")
    html = (
        make_client()
        .get("/vulnerabilities/", query_string={"sort": "total", "dir": "asc"})
        .data.decode()
    )
    assert html.index("web-01") < html.index("web-02")


def test_global_page_paginates_fifty_per_page(monkeypatch):
    rows = {f"mine-{i:03d}": _summary(f"mine-{i:03d}", i) for i in range(60)}
    monkeypatch.setattr("overstate_ui.vulns.queue_or_none", lambda *a, **k: None)
    monkeypatch.setattr("overstate_ui.vulns.mine_get_now", lambda *a, **k: rows)
    page1 = make_client().get("/vulnerabilities/").data.decode()
    assert "mine-059" in page1 and "mine-000" not in page1
    page2 = (
        make_client().get("/vulnerabilities/", query_string={"page": 2}).data.decode()
    )
    assert "mine-000" in page2 and "mine-059" not in page2


def test_minion_tab_renders_detail_report():
    html = make_client().get("/minions/web-01?tab=vulns").data.decode()
    assert "Vulns" in html
    assert "CVE-2026-0000" in html
    assert "fedora:42" in html
    assert "3" in html  # total findings card


def test_minion_tab_searches_cve():
    html = (
        make_client()
        .get("/minions/web-01", query_string={"tab": "vulns", "q": "CVE-2026-0001"})
        .data.decode()
    )
    assert "CVE-2026-0001" in html
    assert "CVE-2026-0000" not in html


def test_minion_tab_falls_back_to_combined_summary():
    html = make_client().get("/minions/web-02?tab=vulns").data.decode()
    assert "CVE-2026-0000" in html  # top list from summary fallback


def test_minion_tab_empty_state_without_data():
    html = make_client().get("/minions/web-09?tab=vulns").data.decode()
    assert "grype-cve-scan" in html
    assert "mine-update" not in html  # mine.refresh cannot refill this key

"""Configurable scheduled file sync and Settings validation tests."""

import datetime as dt
import pathlib
import subprocess

import pytest
import yaml

from overstate_ui import create_app
from overstate_ui.auth import seed_admin
from overstate_ui.config import TestConfig
from overstate_ui.db import create_all, get_session, init_db
from overstate_ui.file_sync_schedule import (
    FILE_SYNC_CRON_DEFAULT,
    FILE_SYNC_CRON_TICK,
    LAST_MESSAGE_SETTING,
    LAST_RUN_SETTING,
    LAST_SLOT_SETTING,
    LAST_STATUS_SETTING,
    cron_matches,
    due_slot,
    parse_cron,
    run_scheduled_sync,
)
from overstate_ui.models import AuditEvent, Setting
from overstate_ui.settings import get_setting

UTC = dt.timezone.utc
REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path):
    init_db("sqlite://")
    app = create_app(TestConfig)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["FILE_ROOTS"] = str(tmp_path)
    with app.app_context():
        create_all()
        seed_admin(password="pw")
    browser = app.test_client()
    browser.post("/login", data={"username": "admin", "password": "pw"})
    browser.app = app
    return browser


def _store(key: str, value: str) -> None:
    session = get_session()
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.commit()


def test_cron_supports_lists_ranges_steps_and_weekdays():
    monday = dt.datetime(2026, 10, 12, 8, 35, tzinfo=UTC)
    assert cron_matches("5,35 8-18/2 * * 1-5", monday)
    assert not cron_matches("5,35 8-18/2 * * 1-5", monday.replace(hour=9))
    assert cron_matches("0 0 1 * 1", dt.datetime(2026, 10, 1, tzinfo=UTC))
    assert cron_matches("0 0 1 * 1", dt.datetime(2026, 10, 5, tzinfo=UTC))
    assert not cron_matches("0 0 1 * 1", dt.datetime(2026, 10, 6, tzinfo=UTC))
    assert cron_matches("0 0 * * 7", dt.datetime(2026, 10, 11, tzinfo=UTC))
    assert cron_matches("5/10 * * * *", monday.replace(minute=55))


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "60 * * * *",
        "0 25 * * *",
        "0 0 0 * *",
        "*/0 * * * *",
        "a * * * *",
    ],
)
def test_invalid_cron_expressions_are_rejected(expression):
    with pytest.raises(ValueError):
        parse_cron(expression)


def test_due_slot_allows_one_minute_startup_grace():
    now = dt.datetime(2026, 10, 12, 8, 1, 20, tzinfo=UTC)
    assert due_slot("0 8 * * 1-5", now) == dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    assert due_slot("off", now) is None


def test_schedule_setting_is_editable_and_rejects_invalid_without_overwrite(client):
    page = client.get("/settings/").data.decode()
    assert "file_sync_cron" in page
    assert FILE_SYNC_CRON_DEFAULT in page

    client.post("/settings/", data={"file_sync_cron": "*/10 * * * *"})
    with client.app.app_context():
        assert get_setting("file_sync_cron") == "*/10 * * * *"

    response = client.post(
        "/settings/", data={"file_sync_cron": "61 * * * *"}, follow_redirects=True
    )
    assert b"Invalid file sync schedule" in response.data
    with client.app.app_context():
        assert get_setting("file_sync_cron") == "*/10 * * * *"

    client.post("/settings/", data={"file_sync_cron": "off"})
    with client.app.app_context():
        assert get_setting("file_sync_cron") == "off"


def test_files_page_displays_schedule_and_last_sync(client):
    with client.app.app_context():
        _store("file_sync_cron", "*/20 * * * *")
        _store(LAST_RUN_SETTING, "2026-10-12T08:00:00+00:00")
        _store(LAST_STATUS_SETTING, "success")
        _store(LAST_MESSAGE_SETTING, "file sync succeeded: abc -> def")
        html = client.get("/files/?tab=updates").data.decode()
    assert "Scheduled sync" in html
    assert "*/20 * * * *" in html
    assert "file sync succeeded: abc -&gt; def" in html


def test_scheduled_sync_respects_interval_and_audits_success(client, monkeypatch):
    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    calls = []
    with client.app.app_context():
        _store("file_sync_cron", "0 8 * * 1-5")
        monkeypatch.setattr(
            "overstate_ui.git_sync.git_sync_now",
            lambda: calls.append("git") or {
                "ok": True,
                "old": "abc",
                "new": "def",
                "changed": True,
            },
        )
        monkeypatch.setattr("overstate_ui.files._refresh_fileserver", lambda: None)
        skipped = run_scheduled_sync(now.replace(minute=2))
        assert skipped == {"ran": False, "ok": True, "message": ""}
        assert calls == []

        result = run_scheduled_sync(now)
        assert result["ran"] and result["ok"]
        assert result["status"] == "success"
        assert "abc -> def" in result["message"]
        assert calls == ["git"]
        assert get_setting(LAST_STATUS_SETTING) == "success"
        assert get_setting(LAST_SLOT_SETTING) == now.isoformat()
        assert get_setting(LAST_RUN_SETTING) == now.isoformat()
        assert any(
            row.action == "file-sync:def"
            for row in get_session().query(AuditEvent).all()
        )
        duplicate = run_scheduled_sync(now)
        assert duplicate["ran"] is False
        assert calls == ["git"]


def test_scheduled_sync_pulls_a_real_fast_forward_checkout(
    client, tmp_path, monkeypatch
):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(seed)], check=True, capture_output=True
    )
    subprocess.run(["git", "checkout", "-qb", "main"], cwd=seed, check=True)
    subprocess.run(
        ["git", "config", "user.email", "sync-test@example.invalid"],
        cwd=seed,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "sync test"], cwd=seed, check=True
    )
    (seed / "state.sls").write_text("version: old\n")
    subprocess.run(["git", "add", "state.sls"], cwd=seed, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(checkout)],
        check=True,
        capture_output=True,
    )
    (seed / "state.sls").write_text("version: new\n")
    subprocess.run(
        ["git", "commit", "-am", "update"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=seed,
        check=True,
        capture_output=True,
    )

    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    with client.app.app_context():
        client.app.config["FILE_ROOTS"] = str(checkout)
        _store("file_sync_cron", "0 8 * * 1-5")
        monkeypatch.setattr("overstate_ui.files._refresh_fileserver", lambda: None)
        result = run_scheduled_sync(now)
        assert result["ok"] and result["status"] == "success"
        assert (checkout / "state.sls").read_text() == "version: new\n"
        assert "fileserver refreshed" in result["message"]


def test_failed_sync_is_logged_and_retried_on_job_retry(client, monkeypatch):
    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    attempts = iter(
        [
            {"ok": False, "reason": "remote host unreachable"},
            {"ok": True, "old": "abc", "new": "abc", "changed": False},
        ]
    )
    with client.app.app_context():
        _store("file_sync_cron", "* * * * *")
        monkeypatch.setattr(
            "overstate_ui.git_sync.git_sync_now", lambda: next(attempts)
        )
        first = run_scheduled_sync(now)
        assert first["ran"] and not first["ok"]
        assert get_setting(LAST_STATUS_SETTING) == "failed"
        assert get_setting(LAST_SLOT_SETTING) == ""
        assert any(
            row.action == "file-sync:failed"
            for row in get_session().query(AuditEvent).all()
        )

        retry = run_scheduled_sync(now)
        assert retry["ran"] and retry["ok"]
        assert retry["status"] == "success"


def test_fileserver_refresh_warning_is_retried_next_due_slot(client, monkeypatch):
    calls = iter(
        [
            {"ok": True, "old": "abc", "new": "def", "changed": True},
            {"ok": True, "old": "def", "new": "def", "changed": False},
        ]
    )
    refresh = iter(["salt-api unavailable", None])
    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    with client.app.app_context():
        _store("file_sync_cron", "*/10 * * * *")
        monkeypatch.setattr("overstate_ui.git_sync.git_sync_now", lambda: next(calls))
        monkeypatch.setattr(
            "overstate_ui.files._refresh_fileserver", lambda: next(refresh)
        )
        first = run_scheduled_sync(now)
        assert first["status"] == "warning"
        assert get_setting(LAST_STATUS_SETTING) == "warning"
        second = run_scheduled_sync(now.replace(minute=10))
        assert second["status"] == "success"
        assert get_setting(LAST_STATUS_SETTING) == "success"


def test_file_sync_cronjob_is_wired_and_namespace_scoped():
    k8s = REPO / "deploy" / "kubernetes"
    (manifest,) = yaml.safe_load_all((k8s / "file-sync-cronjob.yaml").read_text())
    assert manifest["kind"] == "CronJob"
    assert manifest["metadata"]["name"] == "file-sync"
    assert manifest["spec"]["schedule"] == FILE_SYNC_CRON_TICK
    assert manifest["spec"]["timeZone"] == "Etc/UTC"
    assert manifest["spec"]["concurrencyPolicy"] == "Forbid"
    template = manifest["spec"]["jobTemplate"]["spec"]["template"]
    assert template["spec"]["automountServiceAccountToken"] is False
    (container,) = template["spec"]["containers"]
    assert container["command"] == ["python", "-m", "overstate_ui.file_sync_cli"]
    assert container["volumeMounts"][0]["mountPath"] == "/srv/states"
    kustomization = (k8s / "kustomization.yaml").read_text()
    assert "namespace: overstate" in kustomization
    assert "file-sync-cronjob.yaml" in kustomization


def test_cli_emits_run_result_and_returns_failure_status(monkeypatch, capsys):
    from flask import Flask

    from overstate_ui import file_sync_cli

    monkeypatch.setattr(file_sync_cli, "create_app", lambda _config: Flask("test"))
    monkeypatch.setattr(file_sync_cli, "create_all", lambda: None)
    monkeypatch.setattr(
        file_sync_cli,
        "run_scheduled_sync",
        lambda: {
            "ran": True,
            "ok": False,
            "message": "file sync failed: remote unavailable",
        },
    )
    assert file_sync_cli.main() == 1
    assert capsys.readouterr().out.strip() == "file sync failed: remote unavailable"

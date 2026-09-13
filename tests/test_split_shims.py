"""Split-module contract: shims re-export canonical implementations.

jobs.py, tasks.py, and minions.py are thin facades over their
*_helpers/*_service/*_queue/*_salt/*_batch siblings. These tests pin
the import paths the rest of the app (and RQ workers) rely on, so a
moved helper can't silently break a caller.
"""

from overstate_ui import (
    jobs,
    jobs_helpers,
    jobs_service,
    minions,
    minions_helpers,
    tasks,
    tasks_batch,
    tasks_queue,
    tasks_salt,
)


def test_jobs_shim_matches_canonical():
    for name in (
        "sort_jobs",
        "suggest_glob",
        "is_test_mode",
        "parse_batch_fields",
        "killable",
        "TGT_TYPES",
        "CONFIRM_FUNS",
        "FLEET_PRESETS",
        "OPERATION_GROUPS",
    ):
        assert getattr(jobs, name) is getattr(jobs_helpers, name), name
    for name in (
        "sync_job",
        "live_returns_now",
        "build_sls_preview",
        "resolve_group_target",
        "launch",
        "resolve_batch_roster",
        "run_batched",
    ):
        assert getattr(jobs, name) is getattr(jobs_service, name), name


def test_tasks_shim_matches_canonical():
    for name in (
        "queue_or_none",
        "wait_for",
        "build_client",
        "app_context",
        "isolated_app",
        "QUEUE_NAME",
    ):
        assert getattr(tasks, name) is getattr(tasks_queue, name), name
    for name in (
        "normalize_versions",
        "fleet_truth_now",
        "probe_capabilities",
        "CAPABILITY_CHECKS",
        "salt_overview_now",
        "show_sls_now",
    ):
        assert getattr(tasks, name) is getattr(tasks_salt, name), name
    for name in (
        "split_roster",
        "run_wave_batch",
        "run_orchestrate_task",
        "request_batch_cancel",
        "batch_cancelled",
    ):
        assert getattr(tasks, name) is getattr(tasks_batch, name), name


def test_minions_shim_matches_canonical():
    for name in (
        "live_roster",
        "normalize_grains",
        "minion_rows",
        "os_icon_slug",
        "parse_beacon_list",
        "build_onboard_script",
        "onboard_inputs",
        "refresh_sync",
    ):
        assert getattr(minions, name) is getattr(minions_helpers, name), name
    # PAGE_SIZES stays defined on the routes module; groups.py imports it there.
    assert minions.PAGE_SIZES == (10, 25, 50)


def test_shim_patch_point_controls_workers(monkeypatch):
    """Patching tasks.build_client steers the batch/salt workers."""
    import overstate_ui.tasks_batch as batch_mod
    import overstate_ui.tasks_salt as salt_mod

    class Stub:
        pass

    # Neither worker module may hold its own build_client binding:
    # lookups must go through the overstate_ui.tasks shim tests patch.
    # (batch_cancelled canonically lives in tasks_batch, but
    # run_wave_batch still resolves it via the shim at call time.)
    assert not hasattr(batch_mod, "build_client")
    assert not hasattr(salt_mod, "build_client")
    monkeypatch.setattr(tasks, "build_client", lambda: Stub())
    assert isinstance(tasks.build_client(), Stub)

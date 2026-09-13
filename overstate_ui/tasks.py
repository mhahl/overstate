"""Background Salt queries over RQ.

Long or fleet-wide Salt calls run in a worker so the request thread
stays fast. Views enqueue with :func:`queue_or_none` and wait briefly
with :func:`wait_for`; when Redis is unreachable the job is None and
the view runs the same code synchronously. Task functions must stay
importable by path and JSON-serializable in and out.

Split from a single 512-line module: queue plumbing lives in
:mod:`overstate_ui.tasks_queue`, per-domain Salt wrappers in
:mod:`overstate_ui.tasks_salt`, batch/orchestrate executors in
:mod:`overstate_ui.tasks_batch`. Everything is re-exported here so
existing ``overstate_ui.tasks.*`` import paths keep working.
"""

from __future__ import annotations

from .tasks_batch import (
    BATCH_CANCEL_TTL,
    _orch_success,
    _parent_state,
    batch_cancel_key,
    batch_cancelled,
    request_batch_cancel,
    run_orchestrate_task,
    run_wave_batch,
    run_wave_batch_task,
    split_roster,
)
from .tasks_queue import (
    CAPABILITY_CACHE_KEY,
    CAPABILITY_TTL,
    JOB_TIMEOUT,
    QUEUE_NAME,
    RESULT_TTL,
    app_context,
    build_client,
    get_redis_client,
    isolated_app,
    queue_or_none,
    read_capability_cache,
    wait_for,
    write_capability_cache,
)
from .tasks_salt import (
    CAPABILITY_CHECKS,
    FUN_DOC_LINES,
    capabilities_task,
    fleet_truth_now,
    fleet_truth_task,
    fun_doc_now,
    fun_index_task,
    list_functions_now,
    mine_get_now,
    mine_get_task,
    normalize_versions,
    probe_capabilities,
    refresh_inventory_task,
    refresh_now,
    salt_overview_now,
    salt_overview_task,
    show_sls_now,
    show_sls_task,
)

__all__ = [
    "BATCH_CANCEL_TTL",
    "CAPABILITY_CACHE_KEY",
    "CAPABILITY_CHECKS",
    "CAPABILITY_TTL",
    "FUN_DOC_LINES",
    "JOB_TIMEOUT",
    "QUEUE_NAME",
    "RESULT_TTL",
    "_orch_success",
    "_parent_state",
    "app_context",
    "batch_cancel_key",
    "batch_cancelled",
    "build_client",
    "capabilities_task",
    "fleet_truth_now",
    "fleet_truth_task",
    "fun_doc_now",
    "fun_index_task",
    "get_redis_client",
    "isolated_app",
    "list_functions_now",
    "mine_get_now",
    "mine_get_task",
    "normalize_versions",
    "probe_capabilities",
    "queue_or_none",
    "read_capability_cache",
    "refresh_inventory_task",
    "refresh_now",
    "request_batch_cancel",
    "run_orchestrate_task",
    "run_wave_batch",
    "run_wave_batch_task",
    "salt_overview_now",
    "salt_overview_task",
    "show_sls_now",
    "show_sls_task",
    "split_roster",
    "wait_for",
    "write_capability_cache",
]

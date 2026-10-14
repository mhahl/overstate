"""pgjsonb_upsert returner tests: idempotent cluster writes, stock passthrough.

The module under test delegates to ``salt.returners.pgjsonb``, but Salt
is not installed in this repo's venv (it exists only inside the master
image), and neither is ``psycopg2`` (the venv carries psycopg v3).
Every ``salt.*`` / ``psycopg2`` name the module touches is therefore
stubbed in ``sys.modules`` before loading the file from disk — the same
importlib pattern as ``tests/test_salt_cluster_patch.py``. What is real:

- the SQL string the module issues (the entire point of the module),
- the parameter order and Json wrapping (must match stock semantics),
- duplicate absorption via ``rowcount`` (no exception, debug log only),
- delegation of every other entry point to upstream, including the
  ``__opts__``/``__salt__`` namespace sync the loader normally owns.
"""

import importlib.util
import logging
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
RETURNER_PY = REPO / "salt-extmods" / "pgjsonb_upsert.py"


class FakeDatabaseError(Exception):
    """Stand-in for psycopg2.DatabaseError (and subclasses)."""


class FakeCursor:
    """Records executes; behaviour knobs per test."""

    def __init__(self, rowcount=1, error=None):
        self.rowcount = rowcount
        self.error = error
        self.executes = []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        if self.error is not None:
            raise self.error


class FakeServ:
    """Context manager mimicking upstream _get_serv(ret, commit=True)."""

    def __init__(self, cursor):
        self.cursor = cursor
        self.calls = []
        self.enter_error = None

    def __call__(self, ret=None, commit=False):
        self.calls.append({"ret": ret, "commit": commit})
        return self

    def __enter__(self):
        # A connect failure in upstream surfaces when the context is
        # entered (SaltMasterError raised before the first yield).
        if self.enter_error is not None:
            raise self.enter_error
        return self.cursor

    def __exit__(self, *exc):
        return False


class UpstreamStub:
    """Fake salt.returners.pgjsonb: records delegation, syncs dunders."""

    def __init__(self, cursor):
        self._get_serv = FakeServ(cursor)
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"{name}-result"

        return _recorder


def _install_stubs(cursor):
    """Install fake salt/psycopg2 modules; return (upstream_stub, saved)."""
    saved = {}
    for key in (
        "salt",
        "salt.exceptions",
        "salt.returners",
        "salt.returners.pgjsonb",
        "salt.utils",
        "salt.utils.data",
        "psycopg2",
        "psycopg2.extras",
    ):
        if key in sys.modules:
            saved[key] = sys.modules[key]

    upstream = UpstreamStub(cursor)

    salt_mod = types.ModuleType("salt")
    exceptions_mod = types.ModuleType("salt.exceptions")

    class SaltMasterError(Exception):
        pass

    exceptions_mod.SaltMasterError = SaltMasterError
    returners_mod = types.ModuleType("salt.returners")
    returners_mod.pgjsonb = upstream
    utils_mod = types.ModuleType("salt.utils")
    data_mod = types.ModuleType("salt.utils.data")
    data_mod.decode = lambda ret: {k: v for k, v in ret.items()}
    psycopg2_mod = types.ModuleType("psycopg2")
    psycopg2_mod.DatabaseError = FakeDatabaseError
    extras_mod = types.ModuleType("psycopg2.extras")
    extras_mod.Json = lambda v: ("Json", v)
    psycopg2_mod.extras = extras_mod
    # Parent-package attribute links, so `from X import Y` resolves
    # through the stubs without touching the real import system.
    salt_mod.returners = returners_mod
    salt_mod.exceptions = exceptions_mod
    salt_mod.utils = utils_mod
    utils_mod.data = data_mod
    returners_mod.pgjsonb = upstream

    sys.modules["salt"] = salt_mod
    sys.modules["salt.exceptions"] = exceptions_mod
    sys.modules["salt.returners"] = returners_mod
    sys.modules["salt.returners.pgjsonb"] = upstream
    sys.modules["salt.utils"] = utils_mod
    sys.modules["salt.utils.data"] = data_mod
    sys.modules["psycopg2"] = psycopg2_mod
    sys.modules["psycopg2.extras"] = extras_mod
    return upstream, saved, SaltMasterError


def _remove_stubs(saved):
    for key in (
        "salt",
        "salt.exceptions",
        "salt.returners",
        "salt.returners.pgjsonb",
        "salt.utils",
        "salt.utils.data",
        "psycopg2",
        "psycopg2.extras",
    ):
        sys.modules.pop(key, None)
    sys.modules.update(saved)


@pytest.fixture()
def loaded():
    """Load the returner with stubbed deps; mimic the loader's dunders."""
    cursor = FakeCursor()
    upstream, saved, SaltMasterError = _install_stubs(cursor)
    try:
        spec = importlib.util.spec_from_file_location(
            "pgjsonb_upsert_under_test", RETURNER_PY
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Salt's loader injects these globals post-import; tests play loader.
        module.__salt__ = {"fake": "loader"}
        module.__opts__ = {"id": "test-master"}
        yield module, upstream, cursor, SaltMasterError
    finally:
        _remove_stubs(saved)


def _ret():
    return {
        "fun": "test.ping",
        "jid": "20261013230402007465",
        "return": True,
        "id": "minecraft-lhm-prod-a135",
        "success": True,
    }


def test_virtual_name_does_not_shadow_stock(loaded):
    module, _, _, _ = loaded
    assert module.__virtualname__ == "pgjsonb_upsert"
    assert module.__virtualname__ != "pgjsonb"


def test_virtual_reports_ready(loaded):
    module, _, _, _ = loaded
    assert module.__virtual__() is True


def test_virtual_reports_missing_deps():
    saved = {}
    for key in list(sys.modules):
        if key == "salt" or key.startswith(("salt.", "psycopg2")):
            saved[key] = sys.modules.pop(key)
    try:
        spec = importlib.util.spec_from_file_location(
            "pgjsonb_upsert_no_deps", RETURNER_PY
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ok, reason = module.__virtual__()
        assert ok is False
        assert "psycopg2" in reason
    finally:
        sys.modules.update(saved)


def test_conflict_clause():
    """The load-bearing assertion: the whole module is this clause."""
    text = RETURNER_PY.read_text(encoding="utf-8")
    assert "ON CONFLICT (jid, id) DO NOTHING" in text


def test_returner_issues_upsert_with_stock_params(loaded):
    module, upstream, cursor, _ = loaded
    module.returner(_ret())
    assert len(cursor.executes) == 1
    sql, params = cursor.executes[0]
    assert "INSERT INTO salt_returns" in sql
    assert "ON CONFLICT (jid, id) DO NOTHING" in sql
    fun, jid, ret_json, mid, success, full_json, _ts = params
    assert (fun, jid, mid, success) == (
        "test.ping",
        "20261013230402007465",
        "minecraft-lhm-prod-a135",
        True,
    )
    assert ret_json == ("Json", True)
    assert full_json[0] == "Json"
    # Commit path preserved from stock: served with commit=True.
    assert upstream._get_serv.calls[0]["commit"] is True


def test_returner_defaults_missing_success_false(loaded):
    module, _, cursor, _ = loaded
    ret = _ret()
    del ret["success"]
    module.returner(ret)
    assert cursor.executes[0][1][4] is False


def test_duplicate_absorbed_without_exception(loaded, caplog):
    module, _, cursor, _ = loaded
    cursor.rowcount = 0  # ON CONFLICT DO NOTHING touched nothing.
    with caplog.at_level(logging.DEBUG, logger="pgjsonb_upsert_under_test"):
        module.returner(_ret())  # Must not raise.
    assert any("duplicate return absorbed" in r.message for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_genuine_db_error_still_logged(loaded, caplog):
    module, _, cursor, _ = loaded
    cursor.error = FakeDatabaseError("not-null violation on fun")
    with caplog.at_level(logging.DEBUG, logger="pgjsonb_upsert_under_test"):
        module.returner(_ret())  # Logged, never raised (stock contract).
    assert any(
        "failed to store return" in r.message and r.levelno >= logging.ERROR
        for r in caplog.records
    )


def test_connection_failure_drops_with_critical(loaded, caplog):
    module, upstream, _, SaltMasterError = loaded
    # Connect failures surface on context entry in upstream.
    upstream._get_serv.enter_error = SaltMasterError("could not connect")
    with caplog.at_level(logging.DEBUG, logger="pgjsonb_upsert_under_test"):
        module.returner(_ret())
    assert any(
        "PostgreSQL unavailable" in r.message and r.levelno == logging.CRITICAL
        for r in caplog.records
    )


def test_everything_else_delegates_with_dunder_sync(loaded):
    module, upstream, _, _ = loaded
    assert module.get_load("jid-1") == "get_load-result"
    assert module.get_jid("jid-1") == "get_jid-result"
    assert module.get_jids() == "get_jids-result"
    assert module.get_minions() == "get_minions-result"
    assert module.get_fun("test.ping") == "get_fun-result"
    assert module.save_load("jid-1", {"load": 1}) == "save_load-result"
    assert module.save_minions("jid-1", ["m"]) == "save_minions-result"
    assert module.prep_jid(passed_jid="jid-9") == "prep_jid-result"
    assert module.event_return([{"tag": "t"}]) == "event_return-result"
    assert module.clean_old_jobs() == "clean_old_jobs-result"
    delegated = {name for name, _, _ in upstream.calls}
    assert {
        "get_load",
        "get_jid",
        "get_jids",
        "get_minions",
        "get_fun",
        "save_load",
        "save_minions",
        "prep_jid",
        "event_return",
        "clean_old_jobs",
    } <= delegated
    # Loader-owned globals were synced into the upstream namespace.
    assert upstream.__opts__ == {"id": "test-master"}
    assert upstream.__salt__ == {"fake": "loader"}

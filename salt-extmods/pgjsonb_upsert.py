"""Idempotent Postgres job-cache returner for multi-master clusters.

Virtual name: ``pgjsonb_upsert``. Drop-in replacement for the stock
:mod:`salt.returners.pgjsonb` returner as ``master_job_cache``.

Problem
-------
Every Salt master that *sees* a minion return runs its configured
``master_job_cache`` writer. In a single-master deployment that is one
write per return and the stock returner's bare ``INSERT`` is fine. In
Overstate's 3-master Raft cluster, one minion return is observed by all
three members: the return lands first on the master the minion is
connected to (that ``INSERT`` wins), then the return event replicates
over the cluster bus (port 4507) to the other two members, whose cache
writers fire the identical ``INSERT`` into the same shared
``salt_returns`` table — whose primary key is ``(jid, id)``. The two
losers raise ``psycopg2.errors.UniqueViolation`` on every return (two
ERROR tracebacks each: one from the ``_get_serv`` context manager, one
from ``returner()``). Error volume therefore scales as
``(masters - 1) x returns`` and will drown master logs as the fleet
grows, while also masking genuine database failures. No data is lost
(the winner's row always lands), but the noise is load-bearing: it
hides real outages.

Fix
---
This module is byte-for-byte the stock ``pgjsonb`` returner except for
the ``salt_returns`` write, which becomes::

    INSERT INTO salt_returns (...)
    VALUES (...)
    ON CONFLICT (jid, id) DO NOTHING

First writer still wins (same effective outcome as today), late
duplicates are absorbed silently, and a ``rowcount == 0`` is logged at
debug level so duplicate volume stays observable without the
tracebacks. This mirrors the precedent upstream already set in the same
file: ``save_load()`` writes ``jids`` with
``ON CONFLICT (jid) DO UPDATE`` for exactly the same reason (duplicate
jids, see salt issue #22171) — the ``salt_returns`` write was simply
never given the same treatment.

Why a separate module instead of patching upstream
--------------------------------------------------
The stock tree is patched at image build time only for cluster-identity
issues (see ``docs/reference/upstream-salt-patches.md``), each with an
exact-string anchor that fails the build on drift. An ``INSERT``-line
anchor would be churn-prone and would silently re-target on every Salt
upgrade. A delegating wrapper keeps the delta to one SQL clause and
inherits all other upstream behavior (``save_load``'s upsert,
``clean_old_jobs``, ``event_return``, getters) automatically.

Why not fixed upstream
----------------------
Upstream returners assume one writer per table (single master, or
active/passive failover). Salt's own clustering routes cache ownership
per entry (Raft + HashRing); a shared external cache with N concurrent
writers is Overstate's topology, not Salt's supported one — and even
the single-master retry variant of this collision is still open
upstream. SaltStack Enterprise avoids it structurally (masters forward
to a single RaaS ingestion service). See ``docs/reference/returner.md``
for the full analysis.

Configuration
-------------
No new credentials. Option lookup is delegated to upstream, whose
prefix is the literal ``returner.pgjsonb.*`` — so the existing
``returner.conf`` (host/port/db/user/pass, flat dotted keys) is reused
unchanged. The only config change is the cache selector::

    master_job_cache: pgjsonb_upsert

That flip is intentionally NOT yet applied: it takes effect on master
restart and requires this file to be present in the image's
``extension_modules`` dir (see ``Containerfile.salt-master``), so image
rebuild first, config flip second. Activating the flip against an image
without this module breaks return storage on every master.

``DO NOTHING`` vs ``DO UPDATE``
------------------------------
``DO NOTHING`` preserves today's effective semantics (first return
wins, original ``alter_time`` kept). ``DO UPDATE`` would let a stale
replicated duplicate overwrite a fresher row and bump ``alter_time``,
which would lie about when the minion actually answered. If write
recency ever matters more than first-seen stability, change the clause
and the ``rowcount`` debug log below — the tests pin the current
choice.
"""

import logging

log = logging.getLogger(__name__)

# Define the module's virtual name. Must differ from "pgjsonb": Salt's
# loader registers returners by virtual name, and shadowing the stock
# module would make `master_job_cache: pgjsonb` silently change meaning
# on every master (including dev masters that never opted in).
__virtualname__ = "pgjsonb_upsert"

try:
    # Imported for delegation, not vendored: every function except
    # returner() below is upstream's object, so upstream fixes
    # (e.g. save_load's ON CONFLICT) keep flowing without a rebase.
    from salt.returners import pgjsonb as _upstream

    try:
        import psycopg2
        import psycopg2.extras

        _HAS_DEPS = True
    except ImportError:
        _HAS_DEPS = False
except ImportError:
    _upstream = None
    _HAS_DEPS = False


def __virtual__():
    if not _HAS_DEPS or _upstream is None:
        return (
            False,
            "Could not import pgjsonb_upsert returner: "
            + "salt.returners.pgjsonb or python-psycopg2 is not installed.",
        )
    return True


def _delegate(name, *args, **kwargs):
    """Call the same-named function on the stock pgjsonb module.

    Upstream functions read Salt's injected ``__opts__``/``__salt__``
    globals from *their own* module namespace. Salt's loader injects
    those dunders into every loaded returner, but only at load time and
    only into modules it loaded itself — when this wrapper is exercised
    in isolation (tests, one-off scripts) the upstream namespace may
    not have them yet, so sync ours across before delegating.
    Assignment is unconditional and idempotent; in production both
    namespaces already carry the loader's values.
    """
    _upstream.__salt__ = __salt__  # noqa: F821 (injected by Salt's loader)
    _upstream.__opts__ = __opts__  # noqa: F821 (injected by Salt's loader)
    return getattr(_upstream, name)(*args, **kwargs)


def returner(ret):
    """Store one minion return, absorbing cluster-replicated duplicates.

    Identical to :func:`salt.returners.pgjsonb.returner` except the
    ``ON CONFLICT (jid, id) DO NOTHING`` clause (see module docstring).
    A conflicting row means another cluster member already stored this
    exact return — that is the expected steady state, not an error, so
    it is logged at debug (with the losing member implied by the master
    id in the log record) instead of twice at exception level.
    """
    import time

    import salt.exceptions
    import salt.utils.data

    try:
        with _upstream._get_serv(ret, commit=True) as cur:
            # Any edit to this statement must preserve the ON CONFLICT
            # clause: it is the entire reason this module exists.
            # Pinned by tests/test_pgjsonb_upsert.py::test_conflict_clause.
            sql = """INSERT INTO salt_returns
                    (fun, jid, return, id, success, full_ret, alter_time)
                    VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s))
                    ON CONFLICT (jid, id) DO NOTHING"""

            cleaned_return = salt.utils.data.decode(ret)
            cur.execute(
                sql,
                (
                    ret["fun"],
                    ret["jid"],
                    psycopg2.extras.Json(cleaned_return["return"]),
                    ret["id"],
                    ret.get("success", False),
                    psycopg2.extras.Json(cleaned_return),
                    time.time(),
                ),
            )
            if cur.rowcount == 0:
                # rowcount 0 under ON CONFLICT DO NOTHING means the row
                # already existed: a fellow cluster member won this
                # return. Debug, not warning — on a healthy 3-master
                # cluster this fires twice per minion return by design.
                log.debug(
                    "pgjsonb_upsert: duplicate return absorbed "
                    "for jid=%s id=%s (already stored by a cluster peer)",
                    ret.get("jid"),
                    ret.get("id"),
                )
    except salt.exceptions.SaltMasterError:
        log.critical(
            "pgjsonb_upsert: PostgreSQL unavailable, dropping return for jid=%s id=%s",
            ret.get("jid"),
            ret.get("id"),
        )
    except psycopg2.DatabaseError:
        # Genuine failures only: integrity violations other than the
        # absorbed (jid, id) conflict, connection loss mid-write, etc.
        log.exception(
            "pgjsonb_upsert: failed to store return for jid=%s id=%s",
            ret.get("jid"),
            ret.get("id"),
        )


# Everything below is stock behavior, delegated so this module cannot
# drift from upstream. master_job_cache exercises save_load/prep_jid/
# save_minions on publish and the get_*/clean_old_jobs family on query
# and expiry; event_return covers the optional event_return path.


def event_return(events):
    """Return events to Postgres (stock behavior)."""
    return _delegate("event_return", events)


def save_load(jid, load, minions=None):
    """Save the publish load for a jid (stock behavior, already upserts)."""
    return _delegate("save_load", jid, load, minions=minions)


def save_minions(jid, minions, syndic_id=None):
    """Included for API consistency (stock no-op)."""
    return _delegate("save_minions", jid, minions, syndic_id=syndic_id)


def get_load(jid):
    """Return the load data for a jid (stock behavior)."""
    return _delegate("get_load", jid)


def get_jid(jid):
    """Return minion returns for a jid (stock behavior)."""
    return _delegate("get_jid", jid)


def get_fun(fun):
    """Return the last call of a function per minion (stock behavior)."""
    return _delegate("get_fun", fun)


def get_jids():
    """Return all job ids (stock behavior)."""
    return _delegate("get_jids")


def get_minions():
    """Return all minion ids with stored returns (stock behavior)."""
    return _delegate("get_minions")


def prep_jid(nocache=False, passed_jid=None):
    """Prepare a JID (stock behavior)."""
    return _delegate("prep_jid", nocache=nocache, passed_jid=passed_jid)


def clean_old_jobs():
    """Archive/expire old returns per keep_jobs_seconds (stock behavior)."""
    return _delegate("clean_old_jobs")

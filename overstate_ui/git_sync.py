"""Git status + sync for the file-roots checkout behind the Files page.

The file browser never edits content; the only git mutation allowed
here is ``fetch`` + ``pull --ff-only`` — the same flags as
``scripts/sync-file-roots.sh``. Anything that is not a clean
fast-forward (diverged branches, dirty tree, missing upstream, not a
checkout at all) refuses with the git reason instead of forcing.
Subprocesses use fixed argv, no shell, no user input, and a bounded
timeout. Runs inline: the RQ worker has no file-roots mount, so
queueing there would only fail elsewhere.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from flask import current_app

GIT_TIMEOUT = 30
LOG_LINES = 10

_sync_lock = threading.Lock()


def _root() -> Path:
    return Path(current_app.config["FILE_ROOTS"]).resolve()


def _run(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Fixed-argv git call in the checkout. None when git itself fails."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=_root(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _is_checkout() -> bool:
    proc = _run("rev-parse", "--git-dir")
    return proc is not None and proc.returncode == 0


def git_status() -> dict:
    """Read-only checkout status. Never raises; failures become reasons."""
    if not _is_checkout():
        return {"ok": False, "reason": "not a git checkout"}
    out: dict = {"ok": True}
    branch = _run("symbolic-ref", "--quiet", "--short", "HEAD")
    out["branch"] = (
        branch.stdout.strip()
        if branch and branch.returncode == 0 and branch.stdout.strip()
        else "detached"
    )
    sha = _run("rev-parse", "--short", "HEAD")
    out["sha"] = sha.stdout.strip() if sha and sha.returncode == 0 else None
    if out["sha"] is None:
        return {"ok": False, "reason": "cannot read HEAD"}
    porcelain = _run("status", "--porcelain")
    if porcelain is None:
        return {"ok": False, "reason": "git status failed"}
    dirty = [line for line in porcelain.stdout.splitlines() if line.strip()]
    out["clean"] = not dirty
    out["dirty_count"] = len(dirty)
    upstream = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream and upstream.returncode == 0 and upstream.stdout.strip():
        out["upstream"] = upstream.stdout.strip()
        counts = _run("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if counts and counts.returncode == 0:
            try:
                ahead, behind = counts.stdout.strip().split()
                out["ahead"], out["behind"] = int(ahead), int(behind)
            except ValueError:
                out["ahead"], out["behind"] = None, None
        else:
            out["ahead"], out["behind"] = None, None
    else:
        out["upstream"] = None
        out["ahead"], out["behind"] = None, None
    log = _run("log", "--oneline", f"-{LOG_LINES}")
    out["log"] = log.stdout.splitlines() if log and log.returncode == 0 else []
    return out


def _failure(proc: subprocess.CompletedProcess[str] | None, fallback: str) -> str:
    if proc is None:
        return fallback
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    return (err[0][:200] if err else fallback) or fallback


def git_sync_now() -> dict:
    """Fetch + pull --ff-only. One at a time; refusals explain, never force."""
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "reason": "a sync is already running"}
    try:
        if not _is_checkout():
            return {"ok": False, "reason": "not a git checkout"}
        fetch = _run("fetch", "--prune")
        if fetch is None or fetch.returncode != 0:
            return {"ok": False, "reason": _failure(fetch, "git fetch failed")}
        old = _run("rev-parse", "--short", "HEAD")
        old_sha = old.stdout.strip() if old and old.returncode == 0 else None
        pull = _run("pull", "--ff-only")
        if pull is None or pull.returncode != 0:
            return {"ok": False, "reason": _failure(pull, "git pull refused")}
        new = _run("rev-parse", "--short", "HEAD")
        new_sha = new.stdout.strip() if new and new.returncode == 0 else None
        return {
            "ok": True,
            "old": old_sha,
            "new": new_sha,
            "changed": old_sha != new_sha,
        }
    finally:
        _sync_lock.release()

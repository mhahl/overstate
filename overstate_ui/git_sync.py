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

import contextlib
import logging
import re
import subprocess
import threading
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 30
LOG_LINES = 10

_sync_lock = threading.Lock()


@contextlib.contextmanager
def _single_flight():
    """One git write at a time; yields False when one is already running."""
    acquired = _sync_lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _sync_lock.release()


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


def is_checkout() -> bool:
    """Public checkout probe for routes that write (edit/save refuse outside)."""
    return _is_checkout()


def git_head() -> str | None:
    """Full HEAD SHA of the checkout, or None when it cannot be read."""
    proc = _run("rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _oneline(value: str, limit: int = 128) -> str:
    """Collapse free text to one safe line for git metadata.

    Usernames reach commit subjects/authors; newlines or angle brackets
    must never survive into argv-adjacent metadata.
    """
    return re.sub(r"[\s<>]+", " ", value or "").strip()[:limit]


def git_commit_file(rel: str, subject: str, author: str) -> dict:
    """Commit the working-tree changes to exactly one file.

    ``rel`` is a ``safe_join``-validated path relative to the checkout,
    passed as a single argv item (never a shell). Identity is fixed
    per-invocation so commits never depend on repo config; the operator
    is recorded as author. Refusals explain, never force.
    """
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        if not _is_checkout():
            return {"ok": False, "reason": "not a git checkout"}
        add = _run("add", "--", rel)
        if add is None or add.returncode != 0:
            return {"ok": False, "reason": _failure(add, "git add failed")}
        who = _oneline(author, 64) or "overstate"
        proc = _run(
            "-c",
            "user.name=overstate",
            "-c",
            "user.email=overstate@localhost",
            "commit",
            "--author",
            f"{who} <overstate@localhost>",
            "-m",
            _oneline(subject, 160),
            "--",
            rel,
        )
        if proc is None or proc.returncode != 0:
            return {"ok": False, "reason": _failure(proc, "git commit failed")}
        new = _run("rev-parse", "--short", "HEAD")
        new_sha = new.stdout.strip() if new and new.returncode == 0 else None
        return {"ok": True, "new": new_sha}


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
    """Fixed failure words for flashes: git stderr can carry remote URLs
    with embedded tokens, so it is logged server-side and never shown."""
    raw = ""
    if proc is not None:
        raw = ((proc.stderr or proc.stdout) or "").strip()
    if raw:
        logger.warning("git command failed: %s", raw.splitlines()[0][:200])
    lowered = raw.lower()
    if "not a git repository" in lowered:
        return "not a git checkout"
    if "diverged" in lowered or "need to merge" in lowered:
        return "branches have diverged"
    if (
        "local changes" in lowered
        or "would be overwritten" in lowered
        or "dirty" in lowered
        or "your branch is ahead" in lowered
    ):
        return "dirty tree or unpushed work"
    if "no upstream" in lowered or "no tracking information" in lowered:
        return "no upstream configured"
    return fallback


def git_sync_now() -> dict:
    """Fetch + pull --ff-only. One at a time; refusals explain, never force."""
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
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


def _tracked_dirty() -> bool | None:
    """True when tracked files have uncommitted changes.

    None when git itself fails. Untracked files never count: they are
    not pushed, so they must not block a push.
    """
    proc = _run("diff", "--quiet")
    if proc is None:
        return None
    return proc.returncode != 0


def _push_failure(proc: subprocess.CompletedProcess[str] | None) -> str:
    """Fixed failure words for push: remote output can carry URLs and
    paths, so it is logged server-side and never shown."""
    raw = ""
    if proc is not None:
        raw = ((proc.stderr or proc.stdout) or "").strip()
    if raw:
        logger.warning("git push failed: %s", raw.splitlines()[0][:200])
    lowered = raw.lower()
    if "not a git repository" in lowered:
        return "not a git checkout"
    if "no upstream" in lowered or "no tracking information" in lowered:
        return "no upstream configured"
    if (
        "permission denied" in lowered
        or "authentication failed" in lowered
        or "could not read username" in lowered
        or "invalid username" in lowered
    ):
        return "push credentials missing or rejected"
    if "fetch first" in lowered or "non-fast-forward" in lowered:
        return "branches have diverged — sync first"
    return "git push refused"


def git_push_now() -> dict:
    """Push local commits upstream. One at a time; refusals explain, never force.

    Dirty tracked trees refuse (push should send exactly the reviewed
    commits); untracked files are ignored. A push that sends nothing
    reports ``sent`` False so the route can say "up to date".
    """
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        if not _is_checkout():
            return {"ok": False, "reason": "not a git checkout"}
        dirty = _tracked_dirty()
        if dirty is None:
            return {"ok": False, "reason": "git status failed"}
        if dirty:
            return {
                "ok": False,
                "reason": "dirty tree — commit or revert local changes first",
            }
        upstream = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream is None or upstream.returncode != 0 or not upstream.stdout.strip():
            return {"ok": False, "reason": "no upstream configured"}
        push = _run("push")
        if push is None or push.returncode != 0:
            return {"ok": False, "reason": _push_failure(push)}
        new = _run("rev-parse", "--short", "HEAD")
        new_sha = new.stdout.strip() if new and new.returncode == 0 else None
        combined = ((push.stderr or "") + (push.stdout or "")).lower()
        sent = "up-to-date" not in combined and "up to date" not in combined
        return {"ok": True, "new": new_sha, "sent": sent}


def git_fetch_now() -> dict:
    """Fetch remote state without touching the working tree.

    Powers the "check for updates" button: refreshes behind/ahead
    counts so the status card answers against the live remote.
    """
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        if not _is_checkout():
            return {"ok": False, "reason": "not a git checkout"}
        fetch = _run("fetch", "--prune")
        if fetch is None or fetch.returncode != 0:
            return {"ok": False, "reason": _failure(fetch, "git fetch failed")}
        return {"ok": True}

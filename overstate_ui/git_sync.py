"""Git status, sync, commit, and push for the shared-states checkout
behind the Files page.

The checkout lives at the srv roots — the *parent* of file roots —
holding the ``salt/`` tree the browser serves (file roots itself) next
to the ``pillar/`` tree the master reads. All git commands run with
file roots as cwd and resolve upward, so a checkout at either level
keeps working; clone and re-clone always (re)build the srv-level
layout, preserving the Overstate-owned ``reactor/`` sibling.

Reads (status) stay free; writes are explicit and gated: ``fetch`` +
``pull --ff-only`` (Sync, same flags as ``scripts/sync-file-roots.sh``),
single-file commits (edit saves), and upstream ``push`` (admin). Anything
that is not clean — diverged branches, dirty tree, missing upstream,
missing push credentials, not a checkout at all — refuses with a fixed
reason instead of forcing. Subprocesses use fixed argv, no shell, no
user-supplied flags or refs, and a bounded timeout. Runs inline: the RQ
worker has no file-roots mount, so queueing there would only fail
elsewhere.

Repo controls (the Files "Repo" tab) extend the same contract to
bootstrap and repair: ``clone`` a missing checkout, ``set-remote`` a
moved origin, ``reset --hard`` to the tracked upstream, and full
``re-clone`` when ``.git`` itself is corrupt. Destruction is gated:
clone confirms before replacing an existing ``salt/`` seed tree, reset
and re-clone both refuse on unpushed commits, and previews name
everything a destructive run would destroy.
Remote URLs pass an allowlist (https and SSH only — credentials ride a
0600 helper file at the srv roots, outside the browsed ``salt/`` tree
and filtered out of status, because the file browser serves everything
under roots to any logged-in user).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 30
CLONE_TIMEOUT = 120
LOG_LINES = 10

CREDENTIALS_NAME = ".git-credentials"

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


def _srv() -> Path:
    """Checkout home: the srv roots whose ``salt/`` child is file roots.

    Holds the ``salt/`` tree, the ``pillar/`` tree the master reads, and
    the Overstate-owned ``reactor/`` sibling that clone/re-clone preserve.
    """
    return _root().parent


def _toplevel() -> Path | None:
    """Enclosing checkout root, or None when file roots hold no checkout."""
    proc = _run("rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return Path(proc.stdout.strip()).resolve()
    except (OSError, ValueError):
        return None


def _pillar_dir() -> Path:
    """Pillar tree: ``pillar/`` beside file roots under the checkout."""
    top = _toplevel()
    return (top / "pillar") if top is not None else (_srv() / "pillar")


def checkout_layout() -> dict:
    """Where the checkout lives and which Salt entry points exist.

    ``salt_top`` answers from file roots in either layout; ``pillar_top``
    needs the srv-level tree, so a legacy checkout at file roots (or a
    bare seed) reports it missing until the repo is (re-)cloned.
    """
    top = _toplevel()
    return {
        "checkout": top is not None,
        "at_roots": top is not None and top == _root(),
        "salt_top": (_root() / "top.sls").is_file(),
        "pillar_top": (_pillar_dir() / "top.sls").is_file(),
    }


def _visible(lines: list[str]) -> list[str]:
    """Porcelain lines the Files surface owns.

    The checkout root sits above file roots, so status can name paths
    outside the browsed tree (``../``). Tracked changes out there (a
    pillar edit) still count — the master serves them. Hidden are the
    token helper (deployment surface, never working tree) and untracked
    siblings like ``reactor/`` (the reset cleaner only runs under file
    roots, so listing them would promise deletions that never happen).
    """
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        path = line[3:].lstrip('"') if len(line) > 3 else line
        if Path(path).name == CREDENTIALS_NAME:
            continue
        if path.startswith("../") and stripped.startswith("??"):
            continue
        kept.append(line)
    return kept


def _add_mode(path: Path, bits: int) -> None:
    """Best-effort permission widening. Symlinks are never followed."""
    try:
        if path.is_symlink():
            return
        os.chmod(path, path.stat().st_mode | bits)
    except OSError:
        pass


def _ensure_world_readable(path: Path) -> bool:
    """Best-effort a+rX over a fresh tree. Never fails the caller.

    Checkouts made under a 027 umask land 640/750, which the non-root
    master workers cannot traverse — the tree then silently vanishes
    from the fileserver. Directories gain o+rX, files o+r.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            for base, dirs, files in os.walk(path):
                for name in dirs:
                    _add_mode(Path(base) / name, 0o555)
                for name in files:
                    _add_mode(Path(base) / name, 0o444)
        elif path.exists():
            _add_mode(path, 0o444)
    except OSError:
        return False
    return True


def paths_changed(old_sha: str | None, new_sha: str | None, marker: str) -> bool:
    """True when the pulled range touches any path containing ``marker``.

    Advisory only (drives the "sync your modules" hint): any failure
    reads as False, never as a sync error.
    """
    if not old_sha or not new_sha or old_sha == new_sha or not marker:
        return False
    proc = _run("diff", "--name-only", old_sha, new_sha)
    if proc is None or proc.returncode != 0:
        return False
    return any(
        marker in Path(line.strip()).parts
        for line in proc.stdout.splitlines()
        if line.strip()
    )


def _run(
    *args: str,
    cwd: Path | None = None,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str] | None:
    """Fixed-argv git call in the checkout (or ``cwd``). None on failure."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or _root()),
            capture_output=True,
            text=True,
            timeout=timeout,
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


def roots_nonempty() -> bool:
    """True when file roots exists and holds any entry. Never raises.

    Clone needs an empty destination; when this is True without a
    checkout, the Repo tab offers re-clone (which clears first) instead
    of letting clone fail again.
    """
    try:
        return _root().exists() and any(_root().iterdir())
    except OSError:
        return False


def git_head() -> str | None:
    """Full HEAD SHA of the checkout, or None when it cannot be read."""
    proc = _run("rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def git_origin() -> str | None:
    """Origin URL of the checkout, or None when there is none."""
    if not _is_checkout():
        return None
    proc = _run("remote", "get-url", "origin")
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
    dirty = [line for line in _visible(porcelain.stdout.splitlines()) if line.strip()]
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
    if "already exists and is not an empty directory" in lowered:
        return "directory not empty — re-clone to replace it"
    if "remote branch" in lowered and "not found" in lowered:
        return "branch not found on the remote"
    if (
        "authentication failed" in lowered
        or "could not read username" in lowered
        or "invalid username" in lowered
    ):
        return "credentials missing or rejected"
    if "could not resolve host" in lowered or "could not resolve hostname" in lowered:
        return "remote host unreachable"
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
        if old_sha != new_sha:
            # Freshly pulled files inherit the checkout umask; normalize
            # so the master workers can always traverse the tree.
            _ensure_world_readable(_toplevel() or _root())
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


# --- Repo controls -----------------------------------------------------

_HTTPS_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?/\S+$")
_SSH_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[A-Za-z0-9._/~$-]+$|^ssh://"
    r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9._/~$-]+$"
)
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+-]{8,256}$")


def validate_repo_url(url: str) -> dict:
    """Allowlist for origin URLs: https and SSH only, no userinfo.

    Credentials travel via the token field into the helper file, never
    embedded in the URL (which would land in ``.git/config``, readable
    through the file browser).
    """
    url = (url or "").strip()
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return {"ok": False, "reason": "enter a remote URL"}
    if "://" in url and not url.startswith(("https://", "ssh://")):
        # file://, ftp://, ext:: games: the scp-like SSH shape would
        # otherwise accept file:///etc/passwd as host "file".
        return {"ok": False, "reason": "only https:// and SSH remote URLs"}
    if url.startswith("https://"):
        netloc = url.split("/", 3)[2] if "/" in url[8:] else ""
        if "@" in netloc:
            return {
                "ok": False,
                "reason": "credentials belong in the token field, not the URL",
            }
        if _HTTPS_RE.match(url) and netloc:
            return {"ok": True, "scheme": "https", "host": netloc}
        return {"ok": False, "reason": "that https URL cannot be used"}
    if _SSH_RE.match(url):
        return {"ok": True, "scheme": "ssh", "host": None}
    return {"ok": False, "reason": "only https:// and SSH remote URLs"}


def validate_branch(branch: str) -> dict:
    """Branch allowlist. Blank means the remote's default branch."""
    if not (branch or "").strip():
        return {"ok": True, "branch": None}
    branch = branch.strip()
    if not _BRANCH_RE.match(branch) or branch.startswith("/"):
        return {"ok": False, "reason": "that branch name cannot be used"}
    return {"ok": True, "branch": branch}


def validate_token(token: str) -> dict:
    """Token shape: opaque, no whitespace, URL-safe — it is interpolated
    into a credential-helper line, so anything outside the charset
    refuses instead of risking line injection."""
    if not token or not _TOKEN_RE.match(token):
        return {"ok": False, "reason": "that token cannot be stored safely"}
    return {"ok": True}


def _credentials_file() -> Path:
    """Beside the checkout, never inside it: the file browser serves
    everything under roots (including ``.git/``) to logged-in users."""
    return _root().parent / CREDENTIALS_NAME


def has_token() -> bool:
    try:
        return _credentials_file().is_file()
    except OSError:
        return False


def save_token(token: str, host: str) -> dict:
    """Persist an https token for one host (0600). Overwrites silently:
    there is exactly one credential slot and the UI says so."""
    check = validate_token(token)
    if not check["ok"]:
        return check
    if not host or any(c.isspace() for c in host):
        return {"ok": False, "reason": "that host cannot be used"}
    try:
        path = _credentials_file()
        path.write_text(f"https://x:{token}@{host}\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return {"ok": False, "reason": "could not store the token"}
    return {"ok": True}


def clear_token() -> dict:
    try:
        _credentials_file().unlink(missing_ok=True)
    except OSError:
        return {"ok": False, "reason": "could not clear the token"}
    if _is_checkout():
        _run("config", "--unset", "credential.helper")
    return {"ok": True}


def _git_clone(dest: Path, url: str, branch: str | None, token: str | None) -> dict:
    """Lock-free clone primitive. Callers validate and serialize."""
    argv: list[str] = []
    if token:
        argv += ["-c", f"credential.helper=store --file {_credentials_file()}"]
    argv += ["clone"]
    if branch:
        argv += ["--branch", branch]
    argv += ["--", url, str(dest)]
    try:
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "reason": "cannot prepare the checkout directory"}
    proc = _run(*argv, cwd=parent, timeout=CLONE_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return {"ok": False, "reason": _failure(proc, "git clone failed")}
    if token or has_token():
        # The helper file lives beside the checkout, so it survives the
        # re-clone nuke — but each fresh .git needs pointing at it.
        configured = _run(
            "config",
            "credential.helper",
            f"store --file {_credentials_file()}",
            cwd=dest,
        )
        if configured is None or configured.returncode != 0:
            return {"ok": False, "reason": "cloned, but credentials did not stick"}
    return {"ok": True}


def _tree_names(tree: Path) -> list[str]:
    """Relative names of everything under ``tree`` (for replace reports)."""
    names = []
    for base, dirs, files in os.walk(tree, followlinks=False):
        for name in dirs + files:
            try:
                names.append(str((Path(base) / name).relative_to(tree)))
            except (OSError, ValueError):
                continue
    return sorted(names)


def _clone(
    url: str,
    branch: str | None,
    token: str | None = None,
    replace: bool = False,
) -> dict:
    """Clone the canonical repo at the srv roots. Lock-free; ``clone_repo``
    validates + serializes.

    The install keeps the Overstate-owned ``reactor/`` sibling and the
    token helper, and replaces an existing ``salt/`` seed tree only with
    ``replace=True`` — without it the caller gets ``confirm_replace``
    naming every doomed file so the UI can ask first. Anything else in
    the way refuses toward re-clone.

    ``file://`` URLs work here (local bare repos in tests, disaster
    recovery from disk) — the public wrapper never lets them through.
    """
    if branch is not None:
        check = validate_branch(branch)
        if not check["ok"] or check["branch"] is None:
            return {"ok": False, "reason": "that branch name cannot be used"}
        branch = check["branch"]
    if _is_checkout():
        return {"ok": False, "reason": "already a git checkout — use re-clone"}
    srv = _srv()
    salt_name = _root().name
    try:
        entries = {p.name for p in srv.iterdir()} if srv.is_dir() else set()
    except OSError:
        return {"ok": False, "reason": "cannot read the checkout directory"}
    if entries - {salt_name, "reactor", CREDENTIALS_NAME}:
        return {"ok": False, "reason": "directory not empty — re-clone to replace it"}
    salt_dir = srv / salt_name
    doomed = _tree_names(salt_dir) if salt_dir.exists() or salt_dir.is_symlink() else []
    if doomed and not replace:
        return {
            "ok": False,
            "reason": f"existing {salt_name}/ tree — confirm to replace it",
            "confirm_replace": doomed,
        }
    staged = _stage_clone(url, branch, token)
    if not staged["ok"]:
        return staged
    try:
        if salt_dir.is_symlink() or salt_dir.is_file():
            salt_dir.unlink()
        elif salt_dir.is_dir():
            shutil.rmtree(salt_dir)
    except OSError:
        _drop_staged(staged["tmp"])
        return {"ok": False, "reason": "cannot clear the old checkout"}
    if not _move_staged(staged["tmp"], srv, keep_existing=frozenset({"reactor"})):
        return {"ok": False, "reason": "cannot install the fresh clone"}
    _ensure_world_readable(srv)
    return {"ok": True, "replaced": doomed}


def clone_repo(url: str, branch: str, token: str | None, replace: bool = False) -> dict:
    """Validated, serialized clone for the Repo tab."""
    valid = validate_repo_url((url or "").strip())
    if not valid["ok"]:
        return valid
    checked = validate_branch(branch or "")
    if not checked["ok"]:
        return checked
    if token:
        if valid["scheme"] != "https":
            return {
                "ok": False,
                "reason": "tokens are for https URLs — SSH uses deploy keys",
            }
        if not validate_token(token)["ok"]:
            return {"ok": False, "reason": "that token cannot be stored safely"}
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        result = _clone(valid_url(url), checked["branch"], token, replace=replace)
        if result["ok"] and token:
            saved = save_token(token, valid["host"])
            if not saved["ok"]:
                return {
                    "ok": False,
                    "reason": "cloned, but the token did not store",
                }
        return result


def valid_url(url: str) -> str:
    """Stripped URL. Separate so routes render back exactly what parsed."""
    return (url or "").strip()


def _ahead_count() -> int | None:
    """Unpushed commits after a fresh fetch. None when unknowable."""
    fetch = _run("fetch", "--prune")
    if fetch is None or fetch.returncode != 0:
        return None
    upstream = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream is None or upstream.returncode != 0 or not upstream.stdout.strip():
        return None
    counts = _run("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts is None or counts.returncode != 0:
        return None
    try:
        ahead, _ = counts.stdout.strip().split()
        return int(ahead)
    except ValueError:
        return None


def _porcelain_lists() -> tuple[list, list] | None:
    """(tracked-dirty rel paths, untracked rel paths). None on failure."""
    proc = _run("status", "--porcelain")
    if proc is None or proc.returncode != 0:
        return None
    dirty, untracked = [], []
    for line in _visible(proc.stdout.splitlines()):
        if line.startswith("?? "):
            untracked.append(line[3:])
        elif line.strip():
            dirty.append(line[3:] if len(line) > 3 else line)
    return dirty, untracked


def reset_preview() -> dict:
    """What ``reset_hard`` would destroy. Fetches first so behind counts
    answer against the live remote; refusals explain, nothing changes."""
    if not _is_checkout():
        return {"ok": False, "reason": "not a git checkout"}
    branch = _run("symbolic-ref", "--quiet", "--short", "HEAD")
    if branch is None or branch.returncode != 0 or not branch.stdout.strip():
        return {"ok": False, "reason": "detached HEAD — checkout a branch first"}
    fetch = _run("fetch", "--prune")
    if fetch is None or fetch.returncode != 0:
        return {"ok": False, "reason": _failure(fetch, "git fetch failed")}
    lists = _porcelain_lists()
    if lists is None:
        return {"ok": False, "reason": "git status failed"}
    dirty, untracked = lists
    upstream = _run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = None
    if upstream and upstream.returncode == 0 and upstream.stdout.strip():
        counts = _run("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if counts and counts.returncode == 0:
            try:
                ahead_s, behind_s = counts.stdout.strip().split()
                ahead, behind = int(ahead_s), int(behind_s)
            except ValueError:
                pass
    return {
        "ok": True,
        "branch": branch.stdout.strip(),
        "upstream": upstream.stdout.strip()
        if upstream and upstream.returncode == 0
        else None,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "untracked": untracked,
    }


def reset_hard(clean_untracked: bool = False) -> dict:
    """Abandon tracked working-tree changes to the tracked upstream.

    Refuses on unpushed commits (they would be destroyed, not merely
    shelved) and without an upstream. Untracked files survive unless
    ``clean_untracked`` is set — a separate, explicit confirm.
    """
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        if not _is_checkout():
            return {"ok": False, "reason": "not a git checkout"}
        branch = _run("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch is None or branch.returncode != 0 or not branch.stdout.strip():
            return {"ok": False, "reason": "detached HEAD — checkout a branch first"}
        ahead = _ahead_count()
        if ahead is None:
            return {"ok": False, "reason": "cannot reach the remote"}
        if ahead > 0:
            return {
                "ok": False,
                "reason": "unpushed commits would be destroyed — push first",
            }
        reset = _run("reset", "--hard", "@{u}")
        if reset is None or reset.returncode != 0:
            return {"ok": False, "reason": _failure(reset, "git reset refused")}
        if clean_untracked:
            clean = _run("clean", "-fd")
            if clean is None or clean.returncode != 0:
                return {"ok": False, "reason": _failure(clean, "git clean refused")}
        _ensure_world_readable(_toplevel() or _root())
        new = _run("rev-parse", "--short", "HEAD")
        new_sha = new.stdout.strip() if new and new.returncode == 0 else None
        return {"ok": True, "new": new_sha}


def srv_nonempty() -> bool:
    """True when the srv roots hold anything at all. Never raises.

    Clone needs a compliant destination; when this is True without a
    checkout, the Repo tab offers re-clone instead of letting clone
    fail again.
    """
    try:
        return _srv().exists() and any(_srv().iterdir())
    except OSError:
        return False


def _clear_dir(path: Path, keep: frozenset[str] = frozenset()) -> bool:
    """Empty a directory in place (never the mountpoint itself).

    ``keep`` names entries that survive — the Overstate-owned
    ``reactor/`` sibling and the token helper beside the salt tree.
    """
    try:
        for entry in path.iterdir():
            if entry.name in keep:
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    except OSError:
        return False
    return True


def _stage_clone(url: str, branch: str | None, token: str | None) -> dict:
    """Clone into a tmp sibling of the srv roots. Lock-free; callers
    validate, serialize, and install. Failures clean up after
    themselves; success returns ``{"ok": True, "tmp": <dir>}``."""
    srv = _srv()
    tmp = srv.parent / (srv.name + ".__clone_tmp")
    if tmp.exists() and not _clear_dir(tmp):
        return {"ok": False, "reason": "cannot clear a previous attempt"}
    if tmp.exists():
        try:
            tmp.rmdir()
        except OSError:
            return {"ok": False, "reason": "cannot clear a previous attempt"}
    cloned = _git_clone(tmp, url, branch, token)
    if not cloned["ok"]:
        _clear_dir(tmp)
        try:
            tmp.rmdir()
        except OSError:
            pass
        return cloned
    return {"ok": True, "tmp": tmp}


def _move_staged(tmp: Path, dest: Path, keep_existing: frozenset[str]) -> bool:
    """Install a staged clone into place. Entries whose name survives in
    ``keep_existing`` stay untouched (the staged copy is dropped)."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for entry in tmp.iterdir():
            target = dest / entry.name
            if entry.name in keep_existing and target.exists():
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                continue
            shutil.move(str(entry), str(target))
        tmp.rmdir()
    except (OSError, shutil.Error):
        return False
    return True


def _drop_staged(tmp: Path) -> None:
    """Best-effort cleanup of a staged clone the caller abandons."""
    _clear_dir(tmp)
    try:
        tmp.rmdir()
    except OSError:
        pass


def _reclone(url: str, branch: str | None, token: str | None = None) -> dict:
    """Destroy the checkout and clone fresh at the srv roots. Lock-free;
    ``reclone_repo`` validates + serializes. Refuses only on unpushed
    commits — everything else (dirty, untracked, corrupt ``.git``,
    foreign files) is what re-clone is for. The ``reactor/`` sibling
    and the token helper survive the swap."""
    if branch is not None:
        check = validate_branch(branch)
        if not check["ok"] or check["branch"] is None:
            return {"ok": False, "reason": "that branch name cannot be used"}
        branch = check["branch"]
    if _is_checkout():
        ahead = _ahead_count()
        if ahead is None:
            return {"ok": False, "reason": "cannot reach the remote"}
        if ahead > 0:
            return {
                "ok": False,
                "reason": "unpushed commits would be destroyed — push first",
            }
    staged = _stage_clone(url, branch, token)
    if not staged["ok"]:
        return staged
    srv = _srv()
    keep = frozenset({"reactor", CREDENTIALS_NAME})
    if srv.exists() and not _clear_dir(srv, keep=keep):
        _drop_staged(staged["tmp"])
        return {"ok": False, "reason": "cannot clear the old checkout"}
    # A staged reactor/ never nests inside the preserved live one: the
    # live tree (edited through the UI) wins and the staged copy drops.
    if not _move_staged(staged["tmp"], srv, keep_existing=frozenset({"reactor"})):
        return {"ok": False, "reason": "cannot install the fresh clone"}
    _ensure_world_readable(srv)
    return {"ok": True}


def reclone_repo(url: str, branch: str, token: str | None) -> dict:
    """Validated, serialized re-clone for the Repo tab."""
    valid = validate_repo_url((url or "").strip())
    if not valid["ok"]:
        return valid
    checked = validate_branch(branch or "")
    if not checked["ok"]:
        return checked
    if token:
        if valid["scheme"] != "https":
            return {
                "ok": False,
                "reason": "tokens are for https URLs — SSH uses deploy keys",
            }
        if not validate_token(token)["ok"]:
            return {"ok": False, "reason": "that token cannot be stored safely"}
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        result = _reclone(valid_url(url), checked["branch"], token)
        if result["ok"] and token:
            saved = save_token(token, valid["host"])
            if not saved["ok"]:
                return {
                    "ok": False,
                    "reason": "re-cloned, but the token did not store",
                }
        return result


def _set_remote(url: str) -> dict:
    """Repoint origin. Lock-free; ``set_remote_origin`` validates +
    serializes. Refuses on dirty tracked trees and unpushed commits:
    repointing under either orphans work onto the wrong remote."""
    if not _is_checkout():
        return {"ok": False, "reason": "not a git checkout"}
    lists = _porcelain_lists()
    if lists is None:
        return {"ok": False, "reason": "git status failed"}
    dirty, _ = lists
    if dirty:
        return {
            "ok": False,
            "reason": "dirty tree — commit or reset first",
        }
    ahead = _ahead_count()
    if ahead is None:
        return {"ok": False, "reason": "cannot reach the remote"}
    if ahead > 0:
        return {
            "ok": False,
            "reason": "unpushed commits — push first",
        }
    proc = _run("remote", "set-url", "origin", url)
    if proc is None or proc.returncode != 0:
        return {"ok": False, "reason": _failure(proc, "git remote refused")}
    return {"ok": True}


def set_remote_origin(url: str) -> dict:
    """Validated, serialized origin repoint for the Repo tab."""
    valid = validate_repo_url((url or "").strip())
    if not valid["ok"]:
        return valid
    with _single_flight() as free:
        if not free:
            return {"ok": False, "reason": "a sync is already running"}
        return _set_remote(valid_url(url))

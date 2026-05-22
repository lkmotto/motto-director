"""Local repo cache via shallow blobless git clones.

Replaces per-file REST round-trips with one git clone (first cycle) + git
pull (subsequent cycles) per repo. Git protocol operations don't count
against GitHub's 5,000/hr REST rate limit, so this scales to many repos
without burning budget.

Reference: GitHub community discussion #44515 confirms git operations are
exempt from the REST rate limit.

Layout:
    /var/cache/motto-director/repos/<owner>/<repo>/

Each repo is cloned with:
    --depth=1 --filter=blob:none

This downloads tree metadata + only the blobs needed for HEAD checkout —
typical clone is 50-300 KB of packfile, completing in 1-3s on a fast uplink.

Disk cost for ~14 small motto repos: well under 200 MB total.

Usage:
    cache = RepoCache()
    cache.ensure("lkmotto/motto-director")  # clone or pull
    text = cache.read_file("lkmotto/motto-director", "director/main.py")
    paths = cache.list_files("lkmotto/motto-director", suffix=".py")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_CACHE_DIR = "/var/cache/motto-director/repos"
DEFAULT_TIMEOUT_S = 60


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _cache_dir() -> Path:
    raw = os.environ.get("DIRECTOR_REPO_CACHE_DIR", DEFAULT_CACHE_DIR)
    return Path(raw)


def _pat() -> str:
    """Return the GitHub PAT used to clone private repos.

    Falls back through the same chain perceive uses for REST: GITHUB_TOKEN,
    then GH_TOKEN. If neither is set, public repos still clone (anon HTTPS).
    """
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _clone_url(repo_slug: str) -> str:
    """Build the HTTPS clone URL with PAT embedded if available."""
    pat = _pat()
    if pat:
        return f"https://x-access-token:{pat}@github.com/{repo_slug}.git"
    return f"https://github.com/{repo_slug}.git"


@dataclass
class RepoCacheStats:
    cloned: int = 0
    pulled: int = 0
    failed: int = 0
    skipped: int = 0


class RepoCache:
    """Manages a directory of shallow blobless local clones.

    Thread-safe per-repo via a lock map; concurrent ensure() of the same
    repo from multiple lenses is serialized.
    """

    def __init__(self, base_dir: Path | None = None):
        self.base = base_dir or _cache_dir()
        self.base.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def repo_path(self, repo_slug: str) -> Path:
        owner, _, name = repo_slug.partition("/")
        return self.base / owner / name

    def _lock_for(self, repo_slug: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(repo_slug)
            if lock is None:
                lock = threading.Lock()
                self._locks[repo_slug] = lock
            return lock

    # ------------------------------------------------------------------
    # Clone / pull
    # ------------------------------------------------------------------

    def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_S,
    ) -> tuple[int, str, str]:
        """Run a git command and return (rc, stdout, stderr)."""
        proc = subprocess.run(  # noqa: S603 - args are constructed locally
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def ensure(self, repo_slug: str) -> bool:
        """Clone if missing, otherwise git pull. Returns True on success.

        Failures are logged but never raise — callers should fall back to
        REST or skip the repo gracefully.
        """
        with self._lock_for(repo_slug):
            path = self.repo_path(repo_slug)
            if not (path / ".git").is_dir():
                return self._clone(repo_slug, path)
            return self._pull(repo_slug, path)

    def _clone(self, repo_slug: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = _clone_url(repo_slug)
        # Sanitize URL for log (strip token)
        safe_url = url.replace(_pat(), "***") if _pat() else url
        started = datetime.now(UTC)
        rc, _stdout, stderr = self._run_git(
            [
                "clone",
                "--depth=1",
                "--filter=blob:none",
                "--single-branch",
                url,
                str(dest),
            ],
            timeout=DEFAULT_TIMEOUT_S,
        )
        elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        if rc != 0:
            _log(
                "repo_cache.clone_failed",
                repo=repo_slug,
                url=safe_url,
                stderr=(stderr or "")[:300],
                elapsed_ms=elapsed_ms,
            )
            return False
        _log(
            "repo_cache.cloned",
            repo=repo_slug,
            elapsed_ms=elapsed_ms,
        )
        return True

    def _pull(self, repo_slug: str, path: Path) -> bool:
        started = datetime.now(UTC)
        rc, _stdout, stderr = self._run_git(
            ["pull", "--depth=1", "--ff-only"],
            cwd=path,
            timeout=DEFAULT_TIMEOUT_S,
        )
        elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        if rc != 0:
            _log(
                "repo_cache.pull_failed",
                repo=repo_slug,
                stderr=(stderr or "")[:300],
                elapsed_ms=elapsed_ms,
            )
            return False
        _log(
            "repo_cache.pulled",
            repo=repo_slug,
            elapsed_ms=elapsed_ms,
        )
        return True

    def ensure_many(self, repo_slugs: list[str]) -> RepoCacheStats:
        """Sequentially ensure a list of repos. Logs aggregate stats."""
        stats = RepoCacheStats()
        started = datetime.now(UTC)
        for slug in repo_slugs:
            path = self.repo_path(slug)
            existed = (path / ".git").is_dir()
            ok = self.ensure(slug)
            if not ok:
                stats.failed += 1
            elif existed:
                stats.pulled += 1
            else:
                stats.cloned += 1
        elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        _log(
            "repo_cache.batch_done",
            cloned=stats.cloned,
            pulled=stats.pulled,
            failed=stats.failed,
            elapsed_ms=elapsed_ms,
            total=len(repo_slugs),
        )
        return stats

    # ------------------------------------------------------------------
    # Read helpers — operate on the local working tree
    # ------------------------------------------------------------------

    def read_file(
        self,
        repo_slug: str,
        rel_path: str,
        *,
        max_bytes: int = 50_000,
    ) -> str:
        """Read a file from the local clone. Returns '' if missing.

        Path is constrained to live inside the clone directory; any attempt
        to escape via .. is rejected.
        """
        root = self.repo_path(repo_slug).resolve()
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root)  # raises if escaped
        except ValueError:
            _log(
                "repo_cache.read_blocked",
                repo=repo_slug,
                rel_path=rel_path,
                reason="path escape",
            )
            return ""
        if not target.is_file():
            return ""
        try:
            data = target.read_bytes()[: max_bytes + 1]
            text = data.decode("utf-8", errors="replace")
            if len(data) > max_bytes:
                text = text[:max_bytes] + "\n... [truncated]"
            return text
        except Exception as exc:  # noqa: BLE001
            _log(
                "repo_cache.read_failed",
                repo=repo_slug,
                rel_path=rel_path,
                error=str(exc)[:200],
            )
            return ""

    def list_files(
        self,
        repo_slug: str,
        *,
        suffix: str | None = None,
        max_files: int = 500,
    ) -> list[str]:
        """List repo-relative paths in the clone, optionally filtered by
        suffix (e.g. '.py'). Skips .git/ and common build dirs."""
        root = self.repo_path(repo_slug)
        if not root.is_dir():
            return []
        skip_dirs = {".git", "node_modules", ".next", "dist", "build", ".venv"}
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # in-place prune
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                if suffix and not fname.endswith(suffix):
                    continue
                full = Path(dirpath) / fname
                try:
                    rel = full.relative_to(root)
                except ValueError:
                    continue
                out.append(str(rel))
                if len(out) >= max_files:
                    return out
        return out

    def recent_commits(
        self,
        repo_slug: str,
        *,
        n: int = 10,
    ) -> list[dict[str, str]]:
        """git log -n {n} --pretty=… Local read; no REST calls."""
        path = self.repo_path(repo_slug)
        if not (path / ".git").is_dir():
            return []
        rc, stdout, stderr = self._run_git(
            [
                "log",
                f"-n{n}",
                "--pretty=format:%h\t%an\t%ad\t%s",
                "--date=short",
            ],
            cwd=path,
            timeout=10,
        )
        if rc != 0:
            _log(
                "repo_cache.log_failed",
                repo=repo_slug,
                stderr=(stderr or "")[:200],
            )
            return []
        out: list[dict[str, str]] = []
        for line in stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, author, date, msg = parts
            out.append({"sha": sha, "author": author, "date": date, "msg": msg})
        return out


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_singleton: RepoCache | None = None


def get_cache() -> RepoCache:
    global _singleton
    if _singleton is None:
        _singleton = RepoCache()
    return _singleton

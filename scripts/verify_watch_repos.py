"""Verify each DEFAULT_WATCH_REPOS slug returns 200 from GitHub.

Exits non-zero with a clear listing if any slug 404s, so CI fails the build
before we deploy a broken default. Reads the token from
WATCH_REPOS_VERIFY_TOKEN (preferred — needs cross-repo read for private repos
in the org) or falls back to GITHUB_TOKEN (CI auto-token; usually only sees
the current repo, so this script will report the others as missing).
"""

from __future__ import annotations

import os
import sys

import httpx

from director.perceive import DEFAULT_WATCH_REPOS

GITHUB_API = "https://api.github.com"


def main() -> int:
    token = os.environ.get("WATCH_REPOS_VERIFY_TOKEN") or os.environ.get(
        "GITHUB_TOKEN"
    )
    if not token:
        print(
            "verify_watch_repos: no WATCH_REPOS_VERIFY_TOKEN or GITHUB_TOKEN set",
            file=sys.stderr,
        )
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    missing: list[tuple[str, int]] = []
    with httpx.Client(timeout=15.0) as client:
        for repo in DEFAULT_WATCH_REPOS:
            r = client.get(f"{GITHUB_API}/repos/{repo}", headers=headers)
            if r.status_code != 200:
                missing.append((repo, r.status_code))

    if missing:
        print(
            "DEFAULT_WATCH_REPOS contains slugs that are not reachable "
            "with the configured token:",
            file=sys.stderr,
        )
        for repo, status in missing:
            print(f"  {repo}: HTTP {status}", file=sys.stderr)
        print(
            "\nFix: correct the slug in director/perceive.py "
            "(DEFAULT_WATCH_REPOS) or grant the verification token access "
            "to the missing private repos.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(DEFAULT_WATCH_REPOS)} default watch repos verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

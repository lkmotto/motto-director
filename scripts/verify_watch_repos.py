"""Verify each DEFAULT_WATCH_REPOS slug returns 200 from GitHub.

Strict mode is gated on WATCH_REPOS_VERIFY_TOKEN. That secret should be a PAT
with cross-repo read access to every default slug; when set, this script
exits non-zero on any 404 so CI fails the build before we deploy a broken
default.

If only the CI auto-injected GITHUB_TOKEN is available, it can only see the
current repo, so 404s on the other org repos are expected. In that case the
script prints a warning and exits 0 — strict verification is opt-in via the
PAT secret, not a default CI requirement.
"""

from __future__ import annotations

import os
import sys

import httpx

from director.perceive import DEFAULT_WATCH_REPOS

GITHUB_API = "https://api.github.com"


def main() -> int:
    pat = os.environ.get("WATCH_REPOS_VERIFY_TOKEN")
    token = pat or os.environ.get("GITHUB_TOKEN")
    strict = bool(pat)

    if not token:
        print(
            "verify_watch_repos: no WATCH_REPOS_VERIFY_TOKEN or GITHUB_TOKEN; "
            "skipping (set WATCH_REPOS_VERIFY_TOKEN as a repo secret to enable "
            "strict verification).",
            file=sys.stderr,
        )
        return 0

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
        if not strict:
            print(
                "verify_watch_repos: WATCH_REPOS_VERIFY_TOKEN is not set; "
                "skipping strict check. Slugs unreachable with the auto "
                f"GITHUB_TOKEN: {[m[0] for m in missing]}",
                file=sys.stderr,
            )
            return 0
        print(
            "DEFAULT_WATCH_REPOS contains slugs that are not reachable "
            "with WATCH_REPOS_VERIFY_TOKEN:",
            file=sys.stderr,
        )
        for repo, status in missing:
            print(f"  {repo}: HTTP {status}", file=sys.stderr)
        print(
            "\nFix: correct the slug in director/perceive.py "
            "(DEFAULT_WATCH_REPOS) or grant the PAT access to the missing "
            "private repos.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(DEFAULT_WATCH_REPOS)} default watch repos verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Compound-PR primitives: one long-lived branch + PR per target repo, into
which the director appends per-tick commits. Optional native GH auto-merge.

Kept deliberately small — uses the REST Contents API for file writes and the
GraphQL API only for the auto-merge mutation (which has no REST equivalent).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

DEFAULT_COMPOUND_BRANCH = "director/auto/compound"
COMPOUND_PR_TITLE = "director: rolling compound PR (auto)"

# Paths in this repo that the director must not touch on its own without
# explicit opt-in. Checked when the target repo is motto-director itself.
SELF_MOD_PREFIXES: tuple[str, ...] = (
    "director/",
    "tests/",
    "scripts/",
    ".github/",
    "Dockerfile",
    "pyproject.toml",
)
SELF_REPO = "lkmotto/motto-director"


@dataclass
class CodeChange:
    path: str
    content: str  # full file content (base64-encoded by Contents API call)


@dataclass
class CompoundEntry:
    """One row in the compound PR's checklist."""

    ts: str
    move_kind: str
    title: str
    rationale: str
    commit_sha: str | None = None


def compound_branch_name() -> str:
    return os.environ.get("DIRECTOR_COMPOUND_BRANCH", DEFAULT_COMPOUND_BRANCH)


def auto_merge_enabled_env() -> bool:
    return os.environ.get("DIRECTOR_AUTO_MERGE", "false").lower() == "true"


def allow_self_mod() -> bool:
    return os.environ.get("DIRECTOR_ALLOW_SELF_MOD", "false").lower() == "true"


def compound_max_moves() -> int:
    try:
        return int(os.environ.get("DIRECTOR_COMPOUND_MAX_MOVES", "10"))
    except ValueError:
        return 10


def is_self_mod(repo: str, paths: list[str]) -> bool:
    if repo != SELF_REPO:
        return False
    return any(p.startswith(SELF_MOD_PREFIXES) for p in paths)


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {os.environ.get('GITHUB_TOKEN') or os.environ.get('GITHUB_PAT', '')}"
        ),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_default_branch_head_sha(client: httpx.Client, repo: str) -> str:
    r = client.get(f"{GITHUB_API}/repos/{repo}", headers=_gh_headers())
    r.raise_for_status()
    default_branch = r.json()["default_branch"]
    rb = client.get(
        f"{GITHUB_API}/repos/{repo}/git/ref/heads/{default_branch}",
        headers=_gh_headers(),
    )
    rb.raise_for_status()
    return rb.json()["object"]["sha"]


def ensure_branch(client: httpx.Client, repo: str, branch: str) -> str:
    """Return the branch's head SHA; create it from default-branch HEAD if
    missing."""
    r = client.get(f"{GITHUB_API}/repos/{repo}/git/ref/heads/{branch}", headers=_gh_headers())
    if r.status_code == 200:
        return r.json()["object"]["sha"]
    if r.status_code != 404:
        r.raise_for_status()
    base_sha = _get_default_branch_head_sha(client, repo)
    cr = client.post(
        f"{GITHUB_API}/repos/{repo}/git/refs",
        headers=_gh_headers(),
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    cr.raise_for_status()
    return base_sha


def append_commit(
    client: httpx.Client,
    repo: str,
    branch: str,
    changes: list[CodeChange],
    message: str,
) -> str | None:
    """Write each change via the Contents API, on top of `branch`. Returns the
    SHA of the last commit created, or None if no changes."""
    last_sha: str | None = None
    for change in changes:
        existing = client.get(
            f"{GITHUB_API}/repos/{repo}/contents/{change.path}",
            params={"ref": branch},
            headers=_gh_headers(),
        )
        body: dict[str, object] = {
            "message": message,
            "branch": branch,
            "content": base64.b64encode(change.content.encode()).decode(),
        }
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        elif existing.status_code != 404:
            existing.raise_for_status()
        r = client.put(
            f"{GITHUB_API}/repos/{repo}/contents/{change.path}",
            headers=_gh_headers(),
            json=body,
        )
        r.raise_for_status()
        last_sha = r.json().get("commit", {}).get("sha")
    return last_sha


def find_compound_pr(client: httpx.Client, repo: str, branch: str) -> dict | None:
    r = client.get(
        f"{GITHUB_API}/repos/{repo}/pulls",
        params={"state": "open", "head": f"{repo.split('/')[0]}:{branch}"},
        headers=_gh_headers(),
    )
    r.raise_for_status()
    items = r.json()
    return items[0] if items else None


def ensure_pr(client: httpx.Client, repo: str, branch: str) -> dict:
    pr = find_compound_pr(client, repo, branch)
    if pr is not None:
        return pr
    base = client.get(f"{GITHUB_API}/repos/{repo}", headers=_gh_headers())
    base.raise_for_status()
    default_branch = base.json()["default_branch"]
    r = client.post(
        f"{GITHUB_API}/repos/{repo}/pulls",
        headers=_gh_headers(),
        json={
            "title": COMPOUND_PR_TITLE,
            "head": branch,
            "base": default_branch,
            "body": _render_pr_body([]),
            "draft": False,
        },
    )
    r.raise_for_status()
    return r.json()


def _render_pr_body(entries: list[CompoundEntry]) -> str:
    if not entries:
        rows = "_No moves appended yet._"
    else:
        rows = "\n".join(
            f"- [ ] `{e.ts}` **{e.move_kind}** — {e.title}\n"
            f"  - rationale: {e.rationale}"
            + (f"\n  - commit: `{e.commit_sha[:7]}`" if e.commit_sha else "")
            for e in entries
        )
    return (
        "Auto-managed by motto-director. Each row below is one move appended "
        "by a director tick.\n\n"
        f"{rows}\n\n"
        "<!-- director-compound-state -->\n"
        f"```json\n{json.dumps([e.__dict__ for e in entries], indent=2)}\n```\n"
        "<!-- /director-compound-state -->\n"
    )


def parse_pr_entries(body: str | None) -> list[CompoundEntry]:
    if not body:
        return []
    start_tag = "<!-- director-compound-state -->"
    end_tag = "<!-- /director-compound-state -->"
    s = body.find(start_tag)
    e = body.find(end_tag)
    if s == -1 or e == -1 or e <= s:
        return []
    block = body[s + len(start_tag) : e]
    json_start = block.find("```json")
    json_end = block.rfind("```")
    if json_start == -1 or json_end <= json_start:
        return []
    raw = block[json_start + len("```json") : json_end].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[CompoundEntry] = []
    for d in data:
        if isinstance(d, dict):
            out.append(
                CompoundEntry(
                    ts=str(d.get("ts", "")),
                    move_kind=str(d.get("move_kind", "")),
                    title=str(d.get("title", "")),
                    rationale=str(d.get("rationale", "")),
                    commit_sha=d.get("commit_sha"),
                )
            )
    return out


def update_pr_body(
    client: httpx.Client, repo: str, pr_number: int, entries: list[CompoundEntry]
) -> None:
    r = client.patch(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        headers=_gh_headers(),
        json={"body": _render_pr_body(entries)},
    )
    r.raise_for_status()


def enable_auto_merge(client: httpx.Client, pr_node_id: str, *, method: str = "SQUASH") -> None:
    """Enable native GH auto-merge via GraphQL. The repo must allow auto-merge."""
    query = (
        "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
        "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
        "    pullRequest { number }"
        "  }"
        "}"
    )
    r = client.post(
        GITHUB_GRAPHQL,
        headers=_gh_headers(),
        json={"query": query, "variables": {"id": pr_node_id, "method": method}},
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data and data["errors"]:
        raise httpx.HTTPError(f"enable_auto_merge failed: {data['errors']}")

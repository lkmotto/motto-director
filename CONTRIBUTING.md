# Contributing to motto-director

## Auto-merge doctrine

PRs labeled **`auto-merge-ok`** will be merged automatically once required CI
checks pass and no protected file is touched. The merge is performed by
`.github/workflows/auto-merge.yaml` via `gh pr merge --squash --delete-branch
--auto`.

### Meta-PRs never carry `auto-merge-ok`

A meta-PR is one that modifies the director's *own* brain — its policy,
observability, perception, ideation, action, or digest layers — or its
deployment plumbing. These changes alter how the director observes,
decides, and acts on every subsequent run, so they require a human
reviewer.

The auto-merge workflow refuses to merge any PR that:

- Has a title containing **`meta:`** or **`self-mod:`** (case-insensitive,
  substring match), OR
- Touches any of these files (path-suffix match):
  - `director/policy.py`
  - `director/observability.py`
  - `director/fleet.py`
  - `director/perceive.py`
  - `director/ideate.py`
  - `director/act.py`
  - `director/digest.py`
  - Any file under `.github/workflows/`
  - `scripts/apply_northflank_crons.py`

When the guard trips, the workflow comments
`Auto-merge skipped: meta-PR safety guard tripped` on the PR and removes the
`auto-merge-ok` label so the PR doesn't re-trigger the guard on every push.

### Filing a `auto-merge-ok` PR

For a normal change (tests, docs, non-protected modules) just open the PR
with the `auto-merge-ok` label. Once CI is green it merges itself. If you
need to land a change to a protected file, open the PR **without** the label
and request human review.

### Verifying the guard locally

The guard's regexes are mirrored in
`.github/workflows/auto-merge-guard-test.sh`. Run it before changing the
workflow:

```bash
bash .github/workflows/auto-merge-guard-test.sh
```

## Conventional Commits

This repository uses [Conventional Commits](https://www.conventionalcommits.org/)
to drive automated releases and changelog generation via
[release-please](https://github.com/googleapis/release-please).

All commits (and squash-merge commit titles for PRs) MUST follow the format:

```
<type>(<optional-scope>): <description>

[optional body]

[optional footer(s)]
```

Common `<type>` values:

| Type       | Purpose                                                    | Triggers release? |
|------------|------------------------------------------------------------|-------------------|
| `feat`     | A new user-visible feature                                 | Yes (minor)       |
| `fix`      | A bug fix                                                  | Yes (patch)       |
| `perf`     | A performance improvement                                  | Yes (patch)       |
| `refactor` | Refactor that doesn't change behavior                      | No                |
| `docs`     | Documentation only                                         | No                |
| `test`     | Adding or correcting tests                                 | No                |
| `build`    | Build system / dependency changes                          | No                |
| `ci`       | CI configuration changes                                   | No                |
| `chore`    | Maintenance work without user impact                       | No                |
| `revert`   | Revert of a previous commit                                | Yes (patch)       |

Breaking changes are signaled by appending `!` after the type/scope
(e.g. `feat!: drop Python 3.8 support`) or by including a
`BREAKING CHANGE:` footer; both trigger a major release.

### How releases work

A GitHub Action (`.github/workflows/release-please.yml`) watches the default
branch. After each merge, it opens (or updates) a "release PR" that
aggregates Conventional Commits, bumps the version, and updates `CHANGELOG.md`.
Merging that release PR cuts a tagged GitHub Release with auto-generated
release notes.

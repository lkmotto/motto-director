#!/usr/bin/env bash
# Local test for the meta-PR guard logic in auto-merge.yaml.
#
# Mirrors the JavaScript regexes from the workflow into shell so we can
# verify, locally and in CI, that protected-file paths and protected
# title prefixes trip the guard. Pure pattern-match — no GitHub calls.
#
# Run: bash .github/workflows/auto-merge-guard-test.sh
# Exits 0 on pass, 1 on fail.

set -euo pipefail

# Mirrors PROTECTED_PATTERNS in auto-merge.yaml.
PROTECTED_REGEX='(^|/)policy\.py$|(^|/)observability\.py$|(^|/)fleet\.py$|(^|/)perceive\.py$|(^|/)ideate\.py$|(^|/)act\.py$|(^|/)digest\.py$|^\.github/workflows/|^scripts/apply_northflank_crons\.py$'

# Mirrors PROTECTED_TITLE_PREFIXES.
TITLE_PREFIXES=("meta:" "self-mod:")

is_protected_file() {
  local f="$1"
  if [[ "$f" =~ $PROTECTED_REGEX ]]; then
    return 0
  fi
  return 1
}

is_protected_title() {
  local t
  t="$(echo "$1" | tr '[:upper:]' '[:lower:]')"
  for p in "${TITLE_PREFIXES[@]}"; do
    if [[ "$t" == *"$p"* ]]; then
      return 0
    fi
  done
  return 1
}

pass=0
fail=0

assert_protected_file() {
  if is_protected_file "$1"; then
    pass=$((pass + 1))
  else
    echo "FAIL: expected protected: $1"
    fail=$((fail + 1))
  fi
}

assert_safe_file() {
  if is_protected_file "$1"; then
    echo "FAIL: expected safe: $1"
    fail=$((fail + 1))
  else
    pass=$((pass + 1))
  fi
}

assert_protected_title() {
  if is_protected_title "$1"; then
    pass=$((pass + 1))
  else
    echo "FAIL: expected protected title: $1"
    fail=$((fail + 1))
  fi
}

assert_safe_title() {
  if is_protected_title "$1"; then
    echo "FAIL: expected safe title: $1"
    fail=$((fail + 1))
  else
    pass=$((pass + 1))
  fi
}

echo "== protected files =="
assert_protected_file "director/policy.py"
assert_protected_file "director/observability.py"
assert_protected_file "director/fleet.py"
assert_protected_file "director/perceive.py"
assert_protected_file "director/ideate.py"
assert_protected_file "director/act.py"
assert_protected_file "director/digest.py"
assert_protected_file ".github/workflows/ci.yml"
assert_protected_file ".github/workflows/auto-merge.yaml"
assert_protected_file "scripts/apply_northflank_crons.py"
# Bare basenames (no dir) also tripped — director/ layout uses them too.
assert_protected_file "policy.py"
assert_protected_file "fleet.py"

echo "== safe files =="
assert_safe_file "director/main.py"
assert_safe_file "director/concurrency.py"
assert_safe_file "director/policy_helpers.py"   # not exactly policy.py
assert_safe_file "tests/test_policy.py"          # tests aren't protected
assert_safe_file "docs/policy.md"
assert_safe_file "scripts/verify_watch_repos.py"
assert_safe_file "README.md"

echo "== protected titles =="
assert_protected_title "meta: weekly self-improvement"
assert_protected_title "META: capitalized still trips"
assert_protected_title "self-mod: bump retry"
assert_protected_title "fix: typo (meta:something)"  # substring match, intentional

echo "== safe titles =="
assert_safe_title "feat: add quality flywheel"
assert_safe_title "fix: ruff format"
assert_safe_title "ci: add auto-merge action with meta-PR guard"  # 'meta-PR' has no colon

echo
echo "passed: $pass  failed: $fail"
[[ "$fail" -eq 0 ]]

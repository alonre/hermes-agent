#!/usr/bin/env bash
# Check upstream/main against main for conflicts, then either merge automatically
# or hand off to a PR for manual resolution.
#
# - Clean merge: open a PR against main and merge it immediately, then update
#   local main to match. main itself is never used for the merge attempt
#   directly, so this can never leave it dirty.
# - Conflicting merge: open a PR from upstream/main into main so you can review
#   and resolve the conflicts yourself (via the PR's "Resolve conflicts" UI, or
#   by checking out the branch locally, merging main into it, fixing conflicts,
#   and pushing). The PR URL is printed and the script exits non-zero; main is
#   left untouched.
#
# Remotes expected:
#   origin   -> your fork   (https://github.com/alonre/hermes-agent.git)
#   upstream -> NousResearch/hermes-agent.git
#
# Requires: gh CLI authenticated with `repo` scope, plus `workflow` if
# .github/workflows/ files change.
set -euo pipefail

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree is not clean; commit or stash your changes first" >&2
  exit 1
fi

git fetch upstream
git fetch origin

if [[ "$(git rev-parse main)" != "$(git rev-parse origin/main)" ]]; then
  echo "error: local main and origin/main have diverged; push or reset local main first" >&2
  exit 1
fi

if git merge-base --is-ancestor upstream/main origin/main; then
  echo "Already up to date with upstream/main."
  exit 0
fi

ORIGINAL_BRANCH="$(git branch --show-current)"
SYNC_BRANCH="sync-upstream-$(date +%Y%m%d-%H%M%S)"

git checkout -b "$SYNC_BRANCH" origin/main

if git merge --no-edit upstream/main; then
  git push -u origin "$SYNC_BRANCH"
  git checkout "$ORIGINAL_BRANCH"
  git branch -D "$SYNC_BRANCH"

  PR_URL="$(gh pr create \
    --base main \
    --head "$SYNC_BRANCH" \
    --title "Sync upstream/main ($(date +%Y-%m-%d))" \
    --body "Automated sync from NousResearch/hermes-agent main. Merged automatically after a conflict-free local check.")"

  echo "Opened PR: $PR_URL"
  gh pr merge "$PR_URL" --merge --delete-branch

  git fetch origin
  if [[ "$ORIGINAL_BRANCH" == "main" ]]; then
    git merge --ff-only origin/main
  else
    git fetch origin main:main
  fi

  echo "main is now up to date with upstream (origin/main: $(git rev-parse --short origin/main))"
else
  git merge --abort
  git checkout "$ORIGINAL_BRANCH"
  git branch -D "$SYNC_BRANCH"

  git branch "$SYNC_BRANCH" upstream/main
  git push -u origin "$SYNC_BRANCH"
  git branch -D "$SYNC_BRANCH"

  PR_URL="$(gh pr create \
    --base main \
    --head "$SYNC_BRANCH" \
    --title "Sync upstream/main - CONFLICTS, needs manual resolution ($(date +%Y-%m-%d))" \
    --body "Automated sync from NousResearch/hermes-agent main hit merge conflicts with main. Resolve them in this PR: checkout $SYNC_BRANCH, merge main into it, fix the conflicts, push, then merge the PR yourself.")"

  echo "error: upstream/main has conflicts with main; opened PR for manual resolution: $PR_URL" >&2
  exit 1
fi

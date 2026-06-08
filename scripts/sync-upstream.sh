#!/usr/bin/env bash
# Pull the latest changes from upstream/main into local main and push to origin (your fork).
#
# Remotes expected:
#   origin   -> your fork   (https://github.com/alonre/hermes-agent.git)
#   upstream -> NousResearch/hermes-agent.git
set -euo pipefail

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree is not clean; commit or stash your changes first" >&2
  exit 1
fi

git fetch upstream
git checkout main
git merge upstream/main
git push origin main

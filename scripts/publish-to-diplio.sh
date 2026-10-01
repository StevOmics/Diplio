#!/usr/bin/env bash
# Publishes reviewed work from `main` to the public Diplio repo (public-origin).
#
# Adds ONE commit on top of the public history holding main's current tree,
# minus ./internal and any AI-tool files (CLAUDE.md, GEMINI.md, .claude/).
# main's own history is never merged or pushed, so nothing that was ever
# committed privately can reach the public remote through history. Run this
# from the private MediaBridge repo, on a clean working tree.
#
# Env: PUBLISH_DRY_RUN=1 builds and checks the commit but does not push.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STRIP_PATHS=(internal CLAUDE.md GEMINI.md .claude)
# Strings that must never appear in the published tree.
FORBIDDEN='housitt|10\.10\.10\.|sdayers|/home/steve'

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree not clean - commit or stash changes before publishing." >&2
  exit 1
fi

git fetch public-origin main

# public-release always mirrors the public remote's main.
if git show-ref --verify --quiet refs/heads/public-release; then
  git branch -f public-release public-origin/main
else
  git branch public-release public-origin/main
fi

git checkout public-release
trap 'git checkout -q -f main' EXIT

# Replace the working tree and index with main's tree (also removes files
# main deleted), then strip the private paths.
git read-tree -u --reset main
for path in "${STRIP_PATHS[@]}"; do
  git rm -r -q --cached --ignore-unmatch "$path" >/dev/null
  rm -rf "$path"
done

# Guards: fail before committing if anything private survived.
for path in "${STRIP_PATHS[@]}"; do
  if [[ -n "$(git ls-files "$path")" ]]; then
    echo "ABORT: $path is still in the index." >&2
    exit 1
  fi
done
if git grep --cached -I -n -E "$FORBIDDEN" -- . ':!scripts/publish-to-diplio.sh' | head -n 5 | grep .; then
  echo "ABORT: private strings found in the tree to be published." >&2
  exit 1
fi

if git diff --cached --quiet; then
  echo "Nothing new to publish."
  exit 0
fi

git commit -q -m "Sync from main ($(date +%Y-%m-%d))"
git --no-pager diff --stat HEAD~1 HEAD | tail -n 5

if [[ "${PUBLISH_DRY_RUN:-}" == "1" ]]; then
  echo "Dry run: built $(git rev-parse --short HEAD) on public-release, not pushed."
  exit 0
fi

git push public-origin public-release:main
echo "Published to https://github.com/StevOmics/Diplio"

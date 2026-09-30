#!/usr/bin/env bash
# Publishes reviewed work from `main` to the public Diplio repo (public-origin).
#
# Merges main into public-release, then hard-excludes ./internal and any
# AI-tool files (CLAUDE.md, GEMINI.md, .claude/) from the result no matter
# what main did to it - these must never reach the public remote. Run this
# from the private MediaBridge repo, on a clean working tree.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree not clean - commit or stash changes before publishing." >&2
  exit 1
fi

git checkout public-release
git merge --no-commit --no-ff main

if [[ -d internal ]]; then
  git rm -r --cached --ignore-unmatch internal >/dev/null
  rm -rf internal
fi

for path in CLAUDE.md GEMINI.md .claude; do
  if [[ -e "$path" ]]; then
    git rm -r --cached --ignore-unmatch "$path" >/dev/null
    rm -rf "$path"
  fi
done

if git diff --cached --quiet; then
  echo "Nothing new to publish."
  git merge --abort 2>/dev/null || true
  git checkout main
  exit 0
fi

git commit -m "Sync from main ($(date +%Y-%m-%d))"
git push public-origin public-release:main
git checkout main

echo "Published to https://github.com/StevOmics/Diplio"

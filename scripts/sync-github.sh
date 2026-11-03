#!/usr/bin/env bash
# Публикует снимок текущего состояния main в GitHub-зеркало одним коммитом
# (без полной истории и чужих email). Обычный `git push` ходит только в
# origin (SourceCraft).
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="gh-snapshot"
REMOTE="github"
GH_NAME="Pavel"
GH_EMAIL="56259474+pashekonline@users.noreply.github.com"

if [ -n "$(git status --porcelain)" ]; then
  echo "Есть незакоммиченные изменения — сначала закоммить или застащь" >&2
  exit 1
fi

BASE=$(git rev-parse --abbrev-ref HEAD)

git checkout --orphan "$BRANCH"
git add -A
GIT_AUTHOR_NAME="$GH_NAME" GIT_AUTHOR_EMAIL="$GH_EMAIL" \
GIT_COMMITTER_NAME="$GH_NAME" GIT_COMMITTER_EMAIL="$GH_EMAIL" \
  git commit -m "Snapshot $(date +%Y-%m-%d)"
git push --force "$REMOTE" "$BRANCH:main"

git checkout "$BASE" >/dev/null
git branch -D "$BRANCH" >/dev/null
echo "GitHub-зеркало обновлено"

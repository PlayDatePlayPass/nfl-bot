#!/usr/bin/env bash
# Publish files to the "data" branch as a single orphan commit, keeping every other file
# that is already there (news.json and rosters.json are written by different workflows).
# Usage: publish_data.sh "commit message" FILE [FILE...]   (files are taken from the cwd)
# Push uses --force-with-lease against the commit we read, retried 3x, so two workflows
# finishing at the same moment can't wipe each other's file.
# Needs GH_TOKEN (the workflow's GITHUB_TOKEN) and GITHUB_REPOSITORY. The token is only
# passed via an http.extraheader env config, never printed.
set -euo pipefail
msg="$1"; shift
src="$PWD"
remote="https://github.com/${GITHUB_REPOSITORY}.git"
auth="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)"
echo "::add-mask::$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)"
for attempt in 1 2 3; do
  rm -rf "$RUNNER_TEMP/pub" && mkdir -p "$RUNNER_TEMP/pub" && cd "$RUNNER_TEMP/pub"
  git init -q -b data
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  lease=""
  if git -c http.extraheader="$auth" fetch -q --depth=1 "$remote" data 2>/dev/null; then
    lease=$(git rev-parse FETCH_HEAD)
    git checkout -q FETCH_HEAD -- . 2>/dev/null || true
  fi
  for f in "$@"; do cp "$src/$f" "./$f"; done
  git add -A
  git commit -q -m "$msg"
  if [ -n "$lease" ]; then lease_arg="--force-with-lease=data:$lease"; else lease_arg="--force"; fi
  if git -c http.extraheader="$auth" push -q "$lease_arg" "$remote" HEAD:data; then
    echo "published $* to data (attempt $attempt; files: $(git ls-files | tr '\n' ' '))"; exit 0
  fi
  echo "push rejected (data branch moved?), retrying"; sleep $((attempt * 5))
done
echo "publish failed"; exit 1

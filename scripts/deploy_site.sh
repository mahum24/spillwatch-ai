#!/bin/bash
# Score live data and deploy the website to GitHub Pages (gh-pages branch).
# The branch is replaced by a single commit each time, so regenerated data never
# accumulates in git history.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/spillwatch publish --out site/data

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -R site/. "$BUILD/"
touch "$BUILD/.nojekyll"
REMOTE=$(git remote get-url origin)
cd "$BUILD"
git init -q -b gh-pages
git add -A
git -c user.name="spillwatch-bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit -q -m "Deploy site $(date -u +%Y-%m-%dT%H:%MZ)"
git push -q --force "$REMOTE" gh-pages
echo "Deployed site"

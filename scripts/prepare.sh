#!/bin/bash
# Resolve the commit pinned for VERSION in versions.json and check out that
# exact commit of the game, shallow, into ~/modbuild/source. Writes COMMIT to
# $GITHUB_ENV for later steps (and echoes the 40-hex sha).
set -euo pipefail

VERSION="${1:?usage: prepare.sh <version>}"
REPO_URL="https://github.com/ProSteve123GH/horizon-android-old.git"
MODBUILD="${MODBUILD:-$HOME/modbuild}"
SRC="$MODBUILD/source"

COMMIT="$(python3 - "$VERSION" <<'PY'
import json, re, sys
version = sys.argv[1]
with open('versions.json') as f:
    versions = json.load(f)
commit = versions.get(version)
if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
    print(f'versions.json has no 40-hex commit for version {version!r}', file=sys.stderr)
    sys.exit(1)
print(commit)
PY
)"

rm -rf "$SRC"
mkdir -p "$SRC"
git -C "$SRC" init -q
git -C "$SRC" remote add origin "$REPO_URL"
git -C "$SRC" fetch --depth 1 origin "$COMMIT"
git -C "$SRC" checkout -q FETCH_HEAD

echo "COMMIT=$COMMIT" >> "${GITHUB_ENV:-/dev/null}"
echo "COMMIT=$COMMIT"

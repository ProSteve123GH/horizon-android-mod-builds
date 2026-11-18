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

# horizon-mixins treats any crate-level cfg_attr as an unresolved cfg gate and
# refuses to index `pub mod game`, so no mod can target horizon_client::game::*.
# This attribute is inert for the Android target this service builds for; drop
# it from the throwaway checkout only (the player's APK is untouched).
python3 - "$SRC/crates/horizon-client/src/lib.rs" <<'PY'
import sys
p = sys.argv[1]
needle = '#![cfg_attr(not(target_os = "android"), forbid(unsafe_code))]\n'
text = open(p).read()
if needle not in text:
    print('prepare: WARNING: expected crate-root cfg_attr not found', file=sys.stderr)
else:
    open(p, 'w').write(text.replace(needle, '', 1))
    print('prepare: removed crate-root cfg_attr (inert on Android)')
PY

# horizon-mixins refuses staged inputs that contain symlinks. Replace every
# symlink with the bytes it points at (this checkout is a throwaway).
find "$SRC" -type l | while IFS= read -r link; do
  if cp -L "$link" "$link.dereferenced" 2>/dev/null; then
    mv "$link.dereferenced" "$link"
    echo "prepare: dereferenced symlink $link"
  else
    rm -f "$link.dereferenced"
    echo "prepare: WARNING: dropping broken symlink $link" >&2
    rm -f "$link"
  fi
done

echo "COMMIT=$COMMIT" >> "${GITHUB_ENV:-/dev/null}"
# And as a step output for workflow expressions (cache keys, expressions).
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "commit=$COMMIT" >> "$GITHUB_OUTPUT"
fi
echo "COMMIT=$COMMIT"

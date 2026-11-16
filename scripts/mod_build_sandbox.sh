#!/bin/bash
# Run the builder inside a sandbox: no network, no new privileges.
#
# The mod's code runs while it compiles (build scripts, proc macros, mixins'
# helpers), so it gets: no network, and read access only to what the build
# needs (~/android, ~/.rustup, ~/.cargo and the system). It may write only
# inside ~/modbuild.
#
# If bubblewrap cannot run on this host (e.g. AppArmor policy that blocks the
# fresh net namespace's loopback setup), fall back to network-only isolation
# and say so loudly in the log: the build still has no network, but it is not
# filesystem-confined.
set -uo pipefail

export MODBUILD="${MODBUILD:-$HOME/modbuild}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_bwrap() {
  bwrap \
    --ro-bind /usr /usr \
    --ro-bind /bin /bin \
    --ro-bind /sbin /sbin \
    --ro-bind /lib /lib \
    --ro-bind /lib64 /lib64 \
    --ro-bind /etc /etc \
    --proc /proc \
    --dev /dev \
    --tmpfs /tmp \
    --bind "$MODBUILD" "$MODBUILD" \
    --ro-bind "$HOME/android" "$HOME/android" \
    --ro-bind "$HOME/.rustup" "$HOME/.rustup" \
    --ro-bind "$HOME/.cargo" "$HOME/.cargo" \
    --setenv MODBUILD "$MODBUILD" \
    --unshare-net \
    --unshare-pid \
    --die-with-parent \
    python3 "$SCRIPT_DIR/mod_build_once.py"
}

if command -v bwrap >/dev/null 2>&1; then
  run_bwrap
  code=$?
  if [ "$code" -eq 0 ]; then
    exit 0
  fi
  if ! sudo -n true 2>/dev/null; then
    echo "sandbox: bwrap exited $code and no passwordless sudo; failing" >&2
    exit "$code"
  fi
  echo "WARNING: bwrap failed ($code); falling back to network-only isolation (NO filesystem confinement)" >&2
  exec sudo -E MODBUILD="$MODBUILD" HOME="$HOME" unshare --net python3 "$SCRIPT_DIR/mod_build_once.py"
fi

echo "WARNING: bwrap not found; falling back to network-only isolation (NO filesystem confinement)" >&2
if sudo -n true 2>/dev/null; then
  exec sudo -E MODBUILD="$MODBUILD" HOME="$HOME" unshare --net python3 "$SCRIPT_DIR/mod_build_once.py"
fi
exec unshare --net python3 "$SCRIPT_DIR/mod_build_once.py"

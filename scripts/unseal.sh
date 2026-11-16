#!/bin/bash
# Download the sealed mod bundle the worker stored in the `requests` release,
# decrypt it with the key from the workflow inputs, and verify the plaintext
# hashes to the job id (its content address). Writes the body to
# ~/modbuild/jobs/current/mods.bin.
#
# Trust boundary: the mod code inside the body is untrusted. Decryption happens
# here only so the sandboxed builder can read it; nothing here runs it.
set -euo pipefail

MODBUILD="${MODBUILD:-$HOME/modbuild}"
EVENT_PATH="${GITHUB_EVENT_PATH:?must run in GitHub Actions (workflow_dispatch)}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"

# Inputs arrive via the event payload, never via the command line.
read -r JOB ASK KEY VERSION < <(python3 - "$EVENT_PATH" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    event = json.load(f)
inputs = event.get('inputs', {})
print(inputs.get('job', ''), inputs.get('ask', ''), inputs.get('key', ''), inputs.get('version', ''))
PY
)

# The key must never reach the log.
echo "::add-mask::$KEY"

[[ "$JOB" =~ ^[0-9a-f]{24}$ ]] || { echo "unseal: job is not 24 lowercase hex" >&2; exit 1; }
[[ "$ASK" =~ ^[0-9a-f]{32}$ ]] || { echo "unseal: ask is not 32 lowercase hex" >&2; exit 1; }
[[ "$KEY" =~ ^[0-9a-f]{64}$ ]] || { echo "unseal: key is not 64 lowercase hex" >&2; exit 1; }
[[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$ ]] || { echo "unseal: bad version" >&2; exit 1; }

COMMIT="${COMMIT:?COMMIT must be set by prepare.sh (via \$GITHUB_ENV)}"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "unseal: COMMIT is not 40 lowercase hex" >&2; exit 1; }

CURRENT="$MODBUILD/jobs/current"
mkdir -p "$CURRENT"
gh release download "$ASK.bin" -R "$REPO" --dir "$CURRENT" --pattern "$ASK.bin" --clobber

# Split iv || ciphertext the worker concatenated.
python3 - "$CURRENT/$ASK.bin" "$CURRENT/iv.hex" "$CURRENT/body.ct" <<'PY'
import sys
data = open(sys.argv[1], 'rb').read()
if len(data) < 17:
    print('unseal: sealed asset too small', file=sys.stderr)
    sys.exit(1)
open(sys.argv[2], 'w').write(data[:16].hex())
open(sys.argv[3], 'wb').write(data[16:])
PY

openssl enc -aes-256-ctr -d -K "$KEY" -iv "$(cat "$CURRENT/iv.hex")" \
  -in "$CURRENT/body.ct" -out "$CURRENT/mods.bin"

# The body is content-addressed: it must hash to the job id.
python3 - "$CURRENT/mods.bin" "$VERSION" "$COMMIT" "$JOB" <<'PY'
import hashlib, sys
body = open(sys.argv[1], 'rb').read()
version, commit, job = sys.argv[2:5]
digest = hashlib.sha256(f'/v1/build?version={version}&commit={commit}'.encode() + body).hexdigest()
if digest[:24] != job:
    print(f'unseal: body hash {digest[:24]} does not match job {job}', file=sys.stderr)
    sys.exit(1)
print(f'unseal: body verified ({len(body)} bytes)')
PY

rm -f "$CURRENT/iv.hex" "$CURRENT/body.ct"

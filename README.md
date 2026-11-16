# Horizon Android mod builds

A personal, unofficial build service for **native mods on the Horizon Android port**
(`horizon-android-old`). Native mods are [horizon-mixins](https://github.com/horizon-rust/horizon-mixins)
bundles: Rust source that patches the game's own code before it is compiled. A phone cannot do that itself,
so when the game's **Compile** button is pressed, it sends the mods to the service here (`worker/`, on
Cloudflare), which asks this repository to compile the game with them. The game then downloads the result
from this repository's `results-*` releases.

**Status:** the code is written and statically checked, but **no build has gone green yet** and the
end-to-end `.hnmod` flow has not been proven. Treat everything here as unverified until the `base` workflow
for version `0.7.7` finishes and a test build publishes a library. See "Honest status" below.

This service is an independent reimplementation. No code was copied from any other project. The architecture
was studied from the public documentation of the Starling project. See [NOTICE](NOTICE) for attribution. The
client-side wiring (the Import/Compile UI and the library swap in `files/native/current.json`) lives in the
game repository, not here.

## What's in the box

| Path | Role |
| --- | --- |
| `worker/` | The service the game talks to. `POST /v1/build?version=<V>` seals a mod bundle (AES-256-CTR, one key per build) into the `requests` release under a random name, then dispatches `build.yml` with the key in the run's inputs. `GET /v1/job/<24-hex>` is the job status; `GET /v1/job/<24-hex>/lib` streams the built library. |
| `build.yml` | Compiles one player's mods. A mod's code runs while it compiles, so that happens in a bubblewrap sandbox with **no network** and read-only filesystem, and that job's token can only read this repository. A second job (`publish`) runs nothing from the mods, verifies and publishes the library. |
| `base.yml` | Builds the clean build cache for one version (runs only trusted code). This is the only workflow that writes the `base-<commit>` cache. |
| `clean.yml` | Every six hours, deletes release assets older than **180 days** and sealed request assets no run took. |
| `versions.json` | Which commit each game version builds against: `{"0.7.7": "<40-hex commit of horizon-android-old>"}`. Empty until the first entry is added. |

## How a build works

1. **Content-addressed jobs.** The job id is the first 24 hex of SHA-256 of `"/v1/build?version=<V>&commit=<C>" + body`,
   where `<C>` is the commit `versions.json` pins for `<V>`. The same mods on the same commit produce the same
   job id, so a build is never done twice: `POST /v1/build` answers from the `results-*` release when
   `<job>.json` (success) or `<job>.failed.json` (settled failure) already exists.
2. **Sealing.** The worker encrypts the body with a fresh AES-256 key and a fresh 16-byte IV
   (`iv || ciphertext`, openssl-compatible `AES-256-CTR`), uploads it to the `requests` release as
   `<32 hex>.bin`, and dispatches `build.yml` with inputs `{job, ask, key, version}`. The key only ever exists
   in the workflow inputs (masked via `::add-mask::`) and in the run's memory.
3. **Verification.** `unseal.sh` decrypts the asset and refuses anything whose hash of
   `"/v1/build?version=<V>&commit=<C>" + body` does not match the job id, or whose `job`/`ask`/`key` are not
   well-formed hex.
4. **Sandbox.** `mod_build_sandbox.sh` runs the builder under `bubblewrap`: no network, no new privileges,
   write access only to `~/modbuild`, read-only to `~/android` (the NDK) and the Rust toolchain. Mod code may
   write only inside `~/modbuild`.
5. **Rebuild from base.** The builder hard-link copies the pristine `~/modbuild/base` into
   `~/modbuild/build-arm64-v8a/target` and compiles `horizon-android` with `--profile fast`. Only what the
   mods changed recompiles.
6. **Publish.** `publish.py` (a separate job, no mod code) checks the library's size and SHA-256 against the
   builder's `status.json`, uploads `<job>.so` + `<job>.json` (or `<job>.failed.json`) to
   `results-<first hex of job>`, and deletes the sealed `requests` asset. This is the service's artifacts:
   releases are public, so builds and logs are public, but the mods are not: the key never leaves the workflow
   inputs, and the sealed asset is deleted after the run.

## Retention and quotas

- Build artifacts and status documents are kept for **180 days** (`CLEAN_DAYS` in `scripts/clean.py`).
- Quotas on `POST /v1/build`: **32 builds per address per UTC day** and **1500 builds globally per UTC day**,
  enforced per worker isolate (in-memory, approximate — GitHub's per-account Actions concurrency is the hard
  backstop). Bodies are capped at **32 MiB**.
- GitHub-hosted runners on a public repository: builds are free; each build is two jobs.

## Security model

- **Mod code is untrusted.** It runs only inside `build.yml`'s sandbox: no network, no filesystem writes outside
  `~/modbuild`, read-only elsewhere (bubblewrap). On hosts where bubblewrap cannot create its net namespace
  (e.g. restrictive AppArmor), the build falls back to network-only isolation with a loud warning in the log —
  no network, but the filesystem is not confined. The build job's token has `contents: read` on this repository
  only.
- **Publishing is separated.** `publish.py` never runs mod code; it only verifies bytes and moves artifacts.
  Its job has `contents: write` so it can upload to releases.
- **The key never persists.** It is generated per request, lives in workflow inputs and process memory, is
  masked in logs, and the sealed asset is deleted by `publish.py` after the run.
- **The game trusts nothing it did not ask for.** The client re-hashes the downloaded library and compares it
  against the status document before selecting it.
- **Public by construction.** Repository, releases, workflow runs and logs are public. Do not put secrets in
  logs or in `versions.json`.
- Native mods built here are **not sandboxed** once installed in the game: they are native code with the
  player's rights. That is inherent to native mods; the red "this code can do anything" warning in the game's
  Compile flow is the actual gate.

## Setup and deploy

Prereqs: a GitHub account with push rights here, a free Cloudflare account with Workers, `gh` authenticated,
`wrangler` 4.x authenticated, and enough Actions storage (each `base-<commit>` cache is roughly 3 GB of a 10 GB
repository quota).

1. **Releases.** The 17 releases the build flows expect: `requests`, `results`, and `results-0` … `results-f`.
   ```
   for tag in requests results results-{0..9} results-{a..f}; do
     gh release create "$tag" -R ProSteve123GH/horizon-android-mod-builds --prerelease --title "$tag" --notes "Reserved for Horizon Android mod build artifacts"
   done
   ```
2. **Build the base cache** for each version in `versions.json`: Actions → `base` → Run workflow → the version
   (`0.7.7`). Multi-hour on first run; every player's build starts from it. Re-run when a version is added.
3. **Deploy the worker** from `worker/`:
   ```
   cd worker
   npm ci --no-audit        # or skip: the tests have no dependencies
   npm test
   gh auth token | wrangler secret put GITHUB_TOKEN
   wrangler deploy
   ```
   `wrangler deploy` prints the service address (`https://horizon-android-mod-builds.<you>.workers.dev`).
4. **Wire the game** (done in the game repo, not here): the dev/launcher side points at the worker URL and
   installs `results-*` artifacts into `native/builds/<id>/<version>/libhorizon_android.so` with a
   `current.json`. That wiring is intentionally not in this repository.

## `versions.json` format

```json
{ "0.7.7": "0123456789abcdef0123456789abcdef01234567" }
```

Keys are game version strings (`[0-9A-Za-z._-]`), values are 40-hex commit SHAs of
`ProSteve123GH/horizon-android-old`. The service pins commits, not branches: any commit is addressable.

## Honest status

- The service **has not yet produced a library**. `base.yml` must go green first (first run of a cold cache can
  take up to ~5 hours; the job is capped at 300 minutes), and only then can a test `.hnmod` prove the full path.
- Native mod builds on this fork are **not yet wired into the game**: the client UI for installing a built
  library (`files/native/current.json`, `native/builds/<id>/<version>/libhorizon_android.so`) is built in the
  game repository and is inert until this service proves out.
- The quotas are per-isolate approximations, not a global ledger.
- `unseal.sh` streams openssl through intermediate files (`iv.hex`, `body.ct` are removed afterwards); the
  plaintext body lands only in `~/modbuild/jobs/current/mods.bin` inside the runner's VM.
- The NDK build path follows the game repo's own `tools/android_build.py` (cargo + NDK clang, no Gradle), which
  is the only configuration this fork ships for.
- The `.hnmod` test fixture (when one is added under `fixtures/`) is a build-canary, not a release artifact; it
  is deleted after validation.

## License and attribution

MIT — see [LICENSE](LICENSE). Attribution and "no code was copied, architecture studied from public docs" are in
[NOTICE](NOTICE). This is a personal, unofficial service; it is not affiliated with Starling, horizon-rust, or
Mojang.

# Test fixture: `hello_mod`

A no-op native mod used to prove the build service end to end. It hooks the `hud` named hook point
(HEAD) and does nothing. It is **not** a release artifact and not wired into any game by itself.

- `hello/horizon-native.json` — the bundle manifest at the `.hnmod` root (id, version, the commit it
  was tested on, `hooks: 1` for the native-hooks API it uses).
- `hello/mixins/hello_mod/horizon-mixin.toml` — the mixins manifest. Its `horizon_revision` pin is a
  placeholder; the builder rewrites it to the commit being built before applying the bundle.
- `hello/mixins/hello_mod/hello.rs` — the one helper the HEAD injection compiles.

Pack it:

```sh
python3 fixtures/pack_hello.py /tmp/hello-build
```

Then POST `/tmp/hello-build/mods.bin` as the body of `POST /v1/build?version=<V>` to the worker. The
response carries the job id; poll `GET /v1/job/<job>` until `state` is `done` or `failed`, and stream
the library from `GET /v1/job/<job>/lib`.

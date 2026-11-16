#!/usr/bin/env python3
"""Build one Horizon Android mod request.

Reads a sealed-then-decrypted mods.bin (a zip) produced by the game, stages
the .hnmod bundles into a source tree that horizon-mixins applies, compiles
horizon-android with profile `fast`, strips the library and writes
result/libhorizon_android.so + result/status.json.

mods.bin layout (made by the client, verified here):

    mods.json   {"files": [{"id": "hello_mod", "version": "0.1.0",
                            "file": "hello_mod.hnmod",
                            "sha256": "<hex of the .hnmod bytes>"}]}
    hello_mod.hnmod   a zip: horizon-native.json + mixins/<id>/...

Rules enforced:
  - every member path is relative, has no "..", and stays under
    mixins/<mod id>/ (plus horizon-native.json at the bundle root);
  - at most 512 members and 64 MiB unpacked per bundle;
  - the manifest id/version match horizon-native.json;
  - the .hnmod bytes match the manifest's sha256 (content address);
  - [compatibility] horizon_revision in each bundle's horizon-mixin.toml is
    rewritten to the commit being built, so mods that pin another revision
    still build against what they run on.

Environment:
  COMMIT            40-hex commit being built (required)
  VERSION           game version being built (required)
  PROFILE           cargo profile (default: fast)
  ABI               arm64-v8a (the only supported ABI)
  MODBUILD          work root (default: ~/modbuild)
  MODS_BIN          the decrypted body (default: ~/modbuild/jobs/current/mods.bin;
                    missing or empty => seed build with no mods, used by base.yml)
  MODBUILD_TARGET   override the cargo target dir (base.yml sets this to ~/modbuild/base)
  RESULT_DIR        where the library and status.json go (default: ~/modbuild/result)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import sys
import zipfile

MIXINS_REV = "ddd9700b21997c95d95ac6ee5ecafe3830cf622a"
NDK_VERSION = "28.2.13676358"
PROFILE = os.environ.get("PROFILE", "fast")
ABI = os.environ.get("ABI", "arm64-v8a")
TRIPLE = {"arm64-v8a": "aarch64-linux-android"}.get(ABI)

MAX_BUNDLE_FILES = 512
MAX_BUNDLE_UNPACKED = 64 * 1024 * 1024
MAX_MOD_COUNT = 32
ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}\.hnmod$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import native_mod_permissions  # noqa: E402


class BuildError(Exception):
    pass


# --- validation helpers (unit-tested) ---------------------------------------


def parse_manifest(data: bytes) -> list[dict]:
    """Parse and validate mods.json. Returns the file list."""
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as e:
        raise BuildError(f"mods.json is not valid JSON: {e}") from e
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or len(files) > MAX_MOD_COUNT:
        raise BuildError(f"mods.json: 'files' must be a list of at most {MAX_MOD_COUNT} entries")
    out = []
    seen = set()
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise BuildError(f"mods.json: entry {i} is not an object")
        mod_id = entry.get("id")
        version = entry.get("version")
        file = entry.get("file")
        sha256 = entry.get("sha256")
        if not isinstance(mod_id, str) or not ID_RE.fullmatch(mod_id):
            raise BuildError(f"mods.json: entry {i}: id must be [a-z0-9_]{{1,64}}")
        if not isinstance(version, str) or not version or len(version) > 64:
            raise BuildError(f"mods.json: entry {i}: bad version")
        if not isinstance(file, str) or not FILE_RE.fullmatch(file):
            raise BuildError(f"mods.json: entry {i}: bad file name")
        if file in seen:
            raise BuildError(f"mods.json: duplicate file {file!r}")
        seen.add(file)
        if not isinstance(sha256, str) or not SHA_RE.fullmatch(sha256):
            raise BuildError(f"mods.json: entry {i}: sha256 must be 64 lowercase hex")
        out.append({"id": mod_id, "version": version, "file": file, "sha256": sha256})
    return out


def _member_path(name: str) -> pathlib.PurePosixPath:
    if not name or "\x00" in name:
        raise BuildError("bundle: empty or NUL-containing member name")
    norm = posixpath.normpath(name)
    if norm.startswith("/") or norm.startswith(".."):
        raise BuildError(f"bundle: member escapes the bundle root: {name!r}")
    parts = pathlib.PurePosixPath(norm).parts
    if any(part in ("..", "") for part in parts):
        raise BuildError(f"bundle: bad member path: {name!r}")
    return pathlib.PurePosixPath(norm)


def validate_hnmod(hnmod_bytes: bytes, expected: dict) -> dict:
    """Validate a .hnmod bundle against its manifest entry.

    Returns {"id", "version", "sha256", "permissions"} for status.json."""
    digest = hashlib.sha256(hnmod_bytes).hexdigest()
    if digest != expected["sha256"]:
        raise BuildError(
            f"bundle {expected.get('file')!r}: sha256 {digest} does not match the manifest"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(hnmod_bytes))
    except zipfile.BadZipFile as e:
        raise BuildError(f"bundle {expected['file']!r}: not a zip: {e}") from e

    total = 0
    members = zf.infolist()
    if len(members) > MAX_BUNDLE_FILES:
        raise BuildError(f"bundle {expected['file']!r}: more than {MAX_BUNDLE_FILES} files")
    normalized = []
    for member in members:
        total += member.file_size
        if total > MAX_BUNDLE_UNPACKED:
            raise BuildError(f"bundle {expected['file']!r}: unpacked size exceeds 64 MiB")
        norm = _member_path(member.filename)
        parts = norm.parts
        is_native_json = norm.as_posix() == "horizon-native.json"
        in_mixins = (
            len(parts) >= 3 and parts[0] == "mixins" and parts[1] == expected["id"]
        )
        if not (is_native_json or in_mixins):
            raise BuildError(
                f"bundle {expected['file']!r}: member outside mixins/{expected['id']}/: "
                f"{member.filename!r}"
            )
        normalized.append(norm)
    if len(set(normalized)) != len(normalized):
        raise BuildError(f"bundle {expected['file']!r}: duplicate member paths")

    native = None
    texts = []
    for member in members:
        name = member.filename
        norm = _member_path(name)
        if norm.as_posix() == "horizon-native.json":
            try:
                native = json.loads(zf.read(member))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise BuildError(f"bundle {expected['file']!r}: horizon-native.json: {e}") from e
        elif name.endswith(".rs"):
            try:
                texts.append(zf.read(member).decode("utf-8", errors="ignore"))
            except Exception:
                pass
    if not isinstance(native, dict):
        raise BuildError(f"bundle {expected['file']!r}: horizon-native.json missing or not an object")
    if native.get("id") != expected["id"]:
        raise BuildError(
            f"bundle {expected['file']!r}: horizon-native.json id {native.get('id')!r} "
            f"!= manifest id {expected['id']!r}"
        )
    if "version" in native and native["version"] != expected["version"]:
        raise BuildError(
            f"bundle {expected['file']!r}: horizon-native.json version {native.get('version')!r} "
            f"!= manifest version {expected['version']!r}"
        )
    return {
        "id": expected["id"],
        "version": expected["version"],
        "sha256": digest,
        "permissions": native_mod_permissions.scan_sources(texts),
    }


def set_horizon_revision(text: str, commit: str) -> str:
    """Rewrite (or add) [compatibility] horizon_revision in a horizon-mixin.toml."""
    if not COMMIT_RE.fullmatch(commit):
        raise BuildError("commit must be 40 lowercase hex")
    if re.search(r"(?m)^\s*horizon_revision\s*=", text):
        return re.sub(
            r"(?m)^(\s*horizon_revision\s*=\s*).*$",
            lambda m: f'{m.group(1)}"{commit}"',
            text,
            count=1,
        )
    m = re.search(r"(?m)^\[compatibility\]\s*$", text)
    if m:
        at = m.end()
        return text[:at] + f'\nhorizon_revision = "{commit}"\n' + text[at:]
    return text.rstrip("\n") + f'\n\n[compatibility]\nhorizon_revision = "{commit}"\n'


def load_mods_bin(mods_bin: pathlib.Path) -> list[tuple[dict, bytes]]:
    """Read + validate mods.bin. Returns [(manifest entry, .hnmod bytes)]."""
    try:
        with zipfile.ZipFile(mods_bin) as zf:
            names = zf.namelist()
            if "mods.json" not in names:
                raise BuildError("mods.bin: mods.json missing")
            entries = parse_manifest(zf.read("mods.json"))
            out = []
            for entry in entries:
                if entry["file"] not in names:
                    raise BuildError(f"mods.bin: {entry['file']!r} missing")
                out.append((entry, zf.read(entry["file"])))
            return out
    except zipfile.BadZipFile as e:
        raise BuildError(f"mods.bin: not a zip: {e}") from e


# --- staging + build ----------------------------------------------------------


def stage_mods(pairs, stage: pathlib.Path) -> list[dict]:
    """Extract each bundle's mixins/<id>/ into stage/<id>/ and rewrite pins."""
    infos = []
    for entry, hnmod_bytes in pairs:
        info = validate_hnmod(hnmod_bytes, entry)
        target = stage / entry["id"]
        target.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(hnmod_bytes)) as zf:
            for member in zf.infolist():
                norm = _member_path(member.filename)
                parts = norm.parts
                if parts[0] == "mixins":
                    dest = target.joinpath(*parts[2:])
                else:  # horizon-native.json: keep a copy beside the bundle
                    dest = target.parent / f"{entry['id']}.horizon-native.json"
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member))
        mixin_toml = target / "horizon-mixin.toml"
        if not mixin_toml.is_file():
            raise BuildError(f"bundle {entry['file']!r}: mixins/{entry['id']}/horizon-mixin.toml missing")
        mixin_toml.write_text(
            set_horizon_revision(mixin_toml.read_text(), os.environ["COMMIT"]),
            encoding="utf-8",
        )
        infos.append(info)
    return infos


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy a tree; hard-link files when possible to keep the base cache intact."""

    def _link_or_copy(s, d, names):
        for name in names:
            s_path, d_path = pathlib.Path(s) / name, pathlib.Path(d) / name
            if s_path.is_dir():
                d_path.mkdir(exist_ok=True)
                _link_or_copy(s_path, d_path, os.listdir(s_path))
            else:
                try:
                    os.link(s_path, d_path)
                except OSError:
                    shutil.copy2(s_path, d_path)

    dst.mkdir(parents=True)
    _link_or_copy(src, dst, os.listdir(src))


def ndk_bin() -> pathlib.Path:
    # tools.sh installs the NDK under ~/android/ndk/<version>. Do not trust
    # ANDROID_HOME here: the runner ships its own (deleted by room.sh).
    base = pathlib.Path(os.environ.get("MODBUILD_ANDROID", str(pathlib.Path.home() / "android")))
    return base / "ndk" / NDK_VERSION / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"


def cargo_env(target_dir: pathlib.Path) -> dict:
    env = dict(os.environ)
    triple = TRIPLE
    ndkc = ndk_bin()
    if not ndkc.is_dir():
        raise BuildError(f"NDK {NDK_VERSION} not found at {ndkc} (run tools.sh)")
    env["PATH"] = f"{ndkc}:{pathlib.Path.home()}/modbuild/cargo/bin:{env.get('PATH', '')}"
    clang = ndkc / f"{triple}28-clang"
    env[f"CC_{triple.replace('-', '_')}"] = str(clang)
    env[f"CXX_{triple.replace('-', '_')}"] = str(ndkc / f"{triple}28-clang++")
    env[f"AR_{triple.replace('-', '_')}"] = str(ndkc / "llvm-ar")
    env[f"CARGO_TARGET_{triple.replace('-', '_').upper()}_LINKER"] = str(clang)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    # No CARGO_NET_OFFLINE here: the seed build (base.yml, has network) must
    # populate ~/modbuild/cargo. The mod build runs sandboxed with no network
    # at all, and the restored base cache holds every locked dependency, so
    # no downloads are attempted there either.
    return env


def horizon_mixins() -> str:
    found = shutil.which("horizon-mixins")
    if found:
        return found
    candidate = pathlib.Path.home() / "modbuild" / "cargo" / "bin" / "horizon-mixins"
    if candidate.is_file():
        return str(candidate)
    raise BuildError("horizon-mixins not on PATH (run tools.sh)")


def tool_env() -> dict:
    """Environment for horizon-mixins: needs cargo on PATH and the toolchain."""
    home = pathlib.Path.home()
    cargo_bin = home / "modbuild" / "cargo" / "bin"
    toolchain = home / ".rustup" / "toolchains" / "1.95.0-x86_64-unknown-linux-gnu" / "bin"
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [str(cargo_bin), str(toolchain), env.get("PATH", "")]
    )
    env["RUSTUP_HOME"] = str(home / ".rustup")
    env["CARGO_HOME"] = str(home / "modbuild" / "cargo")
    return env


def main() -> int:
    commit = os.environ.get("COMMIT", "")
    version = os.environ.get("VERSION", "")
    if not COMMIT_RE.fullmatch(commit):
        print("COMMIT must be 40 lowercase hex", file=sys.stderr)
        return 2
    if not version:
        print("VERSION is required", file=sys.stderr)
        return 2
    if TRIPLE is None:
        print(f"ABI {ABI!r} is not supported (only arm64-v8a)", file=sys.stderr)
        return 2

    modbuild = pathlib.Path(os.environ.get("MODBUILD", str(pathlib.Path.home() / "modbuild")))
    source = modbuild / "source"
    if not (source / "Cargo.toml").is_file():
        print(f"source checkout missing at {source} (run prepare.sh)", file=sys.stderr)
        return 2
    result_dir = pathlib.Path(os.environ.get("RESULT_DIR", str(modbuild / "result")))
    result_dir.mkdir(parents=True, exist_ok=True)

    mods_bin = pathlib.Path(
        os.environ.get("MODS_BIN", str(modbuild / "jobs" / "current" / "mods.bin"))
    )
    stage = modbuild / "jobs" / "current" / "stage"
    mods_stage = modbuild / "jobs" / "current" / "mods-stage"
    for d in (stage, mods_stage):
        shutil.rmtree(d, ignore_errors=True)
    mods_stage.mkdir(parents=True)

    status: dict = {
        "state": "failed",
        "build": {
            "version": version,
            "commit": commit,
            "profile": PROFILE,
            "abi": ABI,
            "mixins": MIXINS_REV,
            "mods": [],
            "permissions": [],
        },
    }
    try:
        pairs = []
        if mods_bin.is_file() and mods_bin.stat().st_size > 0:
            pairs = load_mods_bin(mods_bin)
        mods_info = stage_mods(pairs, mods_stage) if pairs else []
        permissions = sorted({p for info in mods_info for p in info["permissions"]})
        status["build"]["mods"] = [
            {k: info[k] for k in ("id", "version", "sha256")} for info in mods_info
        ]
        status["build"]["permissions"] = permissions

        if pairs:
            print(f"horizon-mixins apply: {len(pairs)} mod(s)")
            subprocess.run(
                [
                    horizon_mixins(),
                    "apply",
                    "--workspace",
                    str(source),
                    "--mods",
                    str(mods_stage),
                    "--out",
                    str(stage),
                ],
                check=True,
                env=tool_env(),
            )
        else:
            print("seed build: no mods, using the source tree directly")
            shutil.rmtree(stage, ignore_errors=True)
            copy_tree(source, stage)

        target_dir = pathlib.Path(
            os.environ.get("MODBUILD_TARGET", str(modbuild / f"build-{ABI}" / "target"))
        )
        base = modbuild / "base"
        if not target_dir.exists() and base.is_dir() and target_dir.resolve() != base.resolve():
            print(f"building from base cache {base} -> {target_dir}")
            copy_tree(base, target_dir)

        print(f"cargo build --profile {PROFILE} -p horizon-android (target {TRIPLE})")
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--profile",
                PROFILE,
                "--target",
                TRIPLE,
                "-p",
                "horizon-android",
            ],
            check=True,
            cwd=stage,
            env=cargo_env(target_dir),
        )

        lib = target_dir / TRIPLE / PROFILE / "libhorizon_android.so"
        if not lib.is_file():
            raise BuildError(f"cargo produced no library at {lib}")
        out_lib = result_dir / "libhorizon_android.so"
        shutil.copyfile(lib, out_lib)
        strip = ndk_bin() / "llvm-strip"
        subprocess.run([str(strip), "--strip-unneeded", str(out_lib)], check=True)

        digest = hashlib.sha256(out_lib.read_bytes()).hexdigest()
        status.update(
            {
                "state": "done",
                "sha256": digest,
                "size": out_lib.stat().st_size,
            }
        )
        (result_dir / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"ok: {out_lib} sha256={digest}")
        return 0
    except Exception as e:  # noqa: BLE001 — any failure must still produce a status
        status["error"] = f"{type(e).__name__}: {e}"
        (result_dir / "status.json").write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"build failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

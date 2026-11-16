#!/usr/bin/env python3
"""Pack fixtures/hello into a .hnmod and a mods.bin the build service accepts.

Usage: python3 fixtures/pack_hello.py [outdir]
Writes hello_mod.hnmod and mods.bin (the request body for POST /v1/build).
"""

import hashlib
import io
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent / "hello"
ID_ = "hello_mod"
VERSION = "0.1.0"


def main() -> int:
    outdir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    outdir.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(ROOT).as_posix())
    hnmod = buf.getvalue()
    hnmod_path = outdir / f"{ID_}.hnmod"
    hnmod_path.write_bytes(hnmod)

    manifest = {
        "files": [
            {
                "id": ID_,
                "version": VERSION,
                "file": hnmod_path.name,
                "sha256": hashlib.sha256(hnmod).hexdigest(),
            }
        ]
    }
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mods.json", json.dumps(manifest))
        zf.write(hnmod_path, hnmod_path.name)
    (outdir / "mods.bin").write_bytes(body.getvalue())
    print(f"wrote {hnmod_path} ({len(hnmod)} bytes)")
    print(f"wrote {outdir / 'mods.bin'} ({(outdir / 'mods.bin').stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

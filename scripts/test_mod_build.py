#!/usr/bin/env python3
"""Pure-Python tests for the mod-validation path and manifest parsing.

No cargo, no network, no NDK. Runs in CI on every push.
"""

import hashlib
import io
import json
import pathlib
import sys
import unittest
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import mod_build_once as mb  # noqa: E402


def hnmod_bytes(mod_id="hello_mod", version="0.1.0", mixin_toml=None, extra_files=None):
    mixin_toml = mixin_toml or (
        '[mod]\nid = "%s"\nversion = "%s"\n\n'
        '[compatibility]\nhorizon_revision = "%s"\n' % (mod_id, version, "0" * 40)
    )
    native = json.dumps({"id": mod_id, "version": version, "horizon": "0" * 40}).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("horizon-native.json", native)
        zf.writestr(f"mixins/{mod_id}/horizon-mixin.toml", mixin_toml)
        zf.writestr(f"mixins/{mod_id}/hello.rs", "pub struct Hello;\n")
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def manifest_for(files):
    return json.dumps({"files": files}).encode()


def mods_bin(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mods.json", manifest_for([e for e, _ in files]))
        for entry_, data in files:
            zf.writestr(entry_["file"], data)
    return buf.getvalue()


def entry(mod_id="hello_mod", version="0.1.0", data=None):
    data = data if data is not None else hnmod_bytes(mod_id, version)
    return (
        {
            "id": mod_id,
            "version": version,
            "file": f"{mod_id}.hnmod",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        data,
    )


class TestParseManifest(unittest.TestCase):
    def test_roundtrip(self):
        e, _ = entry()
        files = mb.parse_manifest(manifest_for([e]))
        self.assertEqual(files, [e])

    def test_rejects_bad_id(self):
        e, _ = entry()
        e = dict(e, id="Bad-Id")
        with self.assertRaises(mb.BuildError):
            mb.parse_manifest(manifest_for([e]))

    def test_rejects_bad_sha(self):
        e, _ = entry()
        e = dict(e, sha256="zz" * 32)
        with self.assertRaises(mb.BuildError):
            mb.parse_manifest(manifest_for([e]))

    def test_rejects_unsafe_file(self):
        e, _ = entry()
        e = dict(e, file="../evil.hnmod")
        with self.assertRaises(mb.BuildError):
            mb.parse_manifest(manifest_for([e]))


class TestValidateHnmod(unittest.TestCase):
    def test_valid_bundle(self):
        e, data = entry()
        info = mb.validate_hnmod(data, e)
        self.assertEqual(info["id"], "hello_mod")
        self.assertEqual(info["version"], "0.1.0")
        self.assertEqual(info["permissions"], [])

    def test_sha_mismatch_refused(self):
        e, data = entry()
        with self.assertRaises(mb.BuildError):
            mb.validate_hnmod(data + b"x", e)

    def test_member_outside_bundle_refused(self):
        e, data = entry()
        with zipfile.ZipFile(io.BytesIO(data)):
            pass
        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            contents = {n: zf.read(n) for n in names}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for n, c in contents.items():
                zf.writestr(n, c)
            zf.writestr("evil.rs", "// not under mixins/<id>/")
        data2 = buf.getvalue()
        e2 = dict(e, sha256=hashlib.sha256(data2).hexdigest())
        with self.assertRaises(mb.BuildError):
            mb.validate_hnmod(data2, e2)

    def test_parent_traversal_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.rs", "x")
        data = buf.getvalue()
        e = dict(entry()[0], sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(mb.BuildError):
            mb.validate_hnmod(data, e)

    def test_too_many_files_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("horizon-native.json", json.dumps({"id": "hello_mod", "version": "0.1.0"}))
            for i in range(mb.MAX_BUNDLE_FILES + 1):
                zf.writestr(f"mixins/hello_mod/f{i}.rs", "//")
        data = buf.getvalue()
        e = dict(entry()[0], sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(mb.BuildError):
            mb.validate_hnmod(data, e)

    def test_native_json_id_mismatch_refused(self):
        data = hnmod_bytes("hello_mod", "0.1.0")
        e = dict(entry()[0], id="other_mod", file="other_mod.hnmod")
        # bundle contents still say hello_mod; manifest claims other_mod
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("horizon-native.json", json.dumps({"id": "hello_mod", "version": "0.1.0"}))
            zf.writestr("mixins/hello_mod/horizon-mixin.toml", "[mod]\n")
        data = buf.getvalue()
        e = dict(e, sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(mb.BuildError):
            mb.validate_hnmod(data, e)

    def test_microphone_permissions_detected(self):
        data = hnmod_bytes(extra_files={"mixins/hello_mod/mic.rs": "use cpal::traits::StreamTrait;\n"})
        e = dict(entry()[0], sha256=hashlib.sha256(data).hexdigest())
        info = mb.validate_hnmod(data, e)
        self.assertEqual(info["permissions"], ["microphone"])


class TestModsBin(unittest.TestCase):
    def test_load_mods_bin_ok(self):
        e, data = entry()
        payload = mods_bin([(e, data)])
        path = pathlib.Path(__file__).parent / ".tmp_mods_bin_test"
        path.write_bytes(payload)
        try:
            pairs = mb.load_mods_bin(path)
            self.assertEqual([p[0]["id"] for p in pairs], ["hello_mod"])
        finally:
            path.unlink(missing_ok=True)

    def test_load_mods_bin_missing_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("something.bin", b"x")
        path = pathlib.Path(__file__).parent / ".tmp_mods_bin_test2"
        path.write_bytes(buf.getvalue())
        try:
            with self.assertRaises(mb.BuildError):
                mb.load_mods_bin(path)
        finally:
            path.unlink(missing_ok=True)


class TestSetHorizonRevision(unittest.TestCase):
    def test_rewrites_existing_pin(self):
        text = '[mod]\nid = "m"\n\n[compatibility]\nhorizon_revision = "%s"\n' % ("1" * 40)
        out = mb.set_horizon_revision(text, "2" * 40)
        self.assertIn(f'horizon_revision = "{"2" * 40}"', out)
        self.assertNotIn("1" * 40, out)

    def test_adds_section_when_missing(self):
        text = '[mod]\nid = "m"\n'
        out = mb.set_horizon_revision(text, "3" * 40)
        self.assertIn("[compatibility]", out)
        self.assertIn(f'horizon_revision = "{"3" * 40}"', out)

    def test_adds_key_to_existing_section(self):
        text = '[mod]\nid = "m"\n\n[compatibility]\nsome_other = "x"\n'
        out = mb.set_horizon_revision(text, "4" * 40)
        self.assertIn(f'horizon_revision = "{"4" * 40}"', out)
        self.assertIn('some_other = "x"', out)


if __name__ == "__main__":
    unittest.main()

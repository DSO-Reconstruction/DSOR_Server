"""Client bundle unpacking (tools/unpack_dso_bundles.py, setup_client_assets.py):
cache paths stay inside the output folder, the CDN payload layers unwrap, and
setup unpacks beside the server's data, never into the client folder."""

import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from dso_server import setup_client_assets as setup
from dso_server.tools import unpack_dso_bundles as unpack


class CachePathTests(unittest.TestCase):
    def test_bundle_and_toc_names_cannot_leave_the_output(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            self.assertEqual(unpack._destination(out, "export_win32/db/static.db4", "ab").name,
                             "export_win32_db_static.db4.ab.nz")
            for name in ("../x", "/etc/passwd", "a/../../x", "c:/x", "a\\b"):
                with self.assertRaises(ValueError, msg=name):
                    unpack._destination(out, name, "ab")
                with self.assertRaises(ValueError, msg=name):
                    unpack._runtime_destination(out, name)
            self.assertEqual(unpack._flat_destination(out, "maps___toc.ab.nz"), out / "maps___toc.ab.nz")
            for name in ("", "..", "a/b", "a\\b", "c:x", "../x"):
                with self.assertRaises(ValueError, msg=name):
                    unpack._flat_destination(out, name)


class PayloadTests(unittest.TestCase):
    def test_nested_cdn_layers_unwrap(self):
        raw = b"SQLite format 3\x00" + bytes(32)
        inner = b"__ZN" + struct.pack("<I", len(raw)) + zlib.compress(raw)
        outer = b"__ZN" + struct.pack("<I", len(inner)) + zlib.compress(inner)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "x.nz"
            path.write_bytes(outer)
            self.assertEqual(setup._read_cached_payload(setup.CacheFile("x", path, "nz")), raw)


def _zn(data):
    return b"__ZN" + struct.pack("<I", len(data)) + zlib.compress(data)


class RuntimeCacheTests(unittest.TestCase):
    def _write(self, files, digests):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cached = {}
            for name, data in files.items():
                path = root / name
                path.write_bytes(data)
                cached[name.rsplit(".", 2)[-2] if name.endswith(".nz") else name.rsplit("._", 1)[1]] = path
            runtime = root / "dlcache"
            written = unpack._write_cached_runtime_assets(runtime, digests, cached, {})
            return written, {
                name: unpack._runtime_destination(runtime, name).read_bytes()
                for name in digests
                if unpack._runtime_destination(runtime, name).is_file()
            }

    def test_legacy_sidecar_hashes_the_wrapped_object(self):
        raw = b"FONTINFO" + bytes(24)
        obj = _zn(raw)
        digest = hashlib.md5(obj).hexdigest()
        written, out = self._write(
            {f"export_win32_ui_chat_unicode0_info.bytes._{digest}": obj},
            {"export/win32/ui/chat_unicode0_info.bytes": digest},
        )
        self.assertEqual(written, 1)
        self.assertEqual(out["export/win32/ui/chat_unicode0_info.bytes"], raw)

    def test_double_wrapped_download_is_written_without_a_zn_header(self):
        raw = b"DDS " + bytes(124)
        inner = _zn(raw)
        digest = hashlib.md5(inner).hexdigest()
        written, out = self._write(
            {f"export_win32_textures_icon.dds.{digest}.nz": _zn(inner)},
            {"export/win32/textures/icon.dds": digest},
        )
        self.assertEqual(written, 1)
        self.assertEqual(out["export/win32/textures/icon.dds"], raw)

    def test_object_matching_no_layer_is_still_skipped(self):
        obj = _zn(b"payload")
        written, out = self._write(
            {"export_win32_data_x.bin._" + "0" * 32: obj},
            {"export/win32/data/x.bin": "0" * 32},
        )
        self.assertEqual((written, out), (0, {}))


def _recording_popen(calls):
    """Stand-in for subprocess.Popen: records argv, prints nothing, exits 0."""

    class FinishedExtractor:
        stdout = iter(())

        def wait(self):
            return 0

    def popen(argv, **_):
        calls.append(argv)
        return FinishedExtractor()

    return popen


class SetupTests(unittest.TestCase):
    def test_bundles_unpack_beside_the_server_data_not_into_the_client(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            client, out = Path(root) / "DSOClient", Path(root) / "server" / "data" / "client_unpacked"
            with patch.object(setup.subprocess, "Popen", side_effect=_recording_popen(calls)):
                self.assertEqual(setup._unpack_client_bundles(client, out), 0)
        argv = calls[0]
        self.assertEqual(argv[argv.index("-o") + 1], str(out))
        self.assertEqual(argv[argv.index("--runtime-cache") + 1], str(out / "dlcache"))
        self.assertNotIn(str(client / "dlcache"), argv)

    def test_unpack_client_flag_unpacks_into_client_cache(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            client = Path(root) / "DSOClient"
            with patch.object(setup.subprocess, "Popen", side_effect=_recording_popen(calls)):
                self.assertEqual(setup._unpack_client_bundles(client, client), 0)
        argv = calls[0]
        self.assertEqual(argv[argv.index("-o") + 1], str(client))
        self.assertEqual(argv[argv.index("--runtime-cache") + 1], str(client / "dlcache"))


if __name__ == "__main__":
    unittest.main()

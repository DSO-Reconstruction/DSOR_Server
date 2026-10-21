"""Client bundle unpacking (tools/unpack_dso_bundles.py, setup_client_assets.py):
cache paths stay inside the output folder, the CDN payload layers unwrap, and
setup unpacks beside the server's data, never into the client folder."""

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


class SetupTests(unittest.TestCase):
    def test_bundles_unpack_beside_the_server_data_not_into_the_client(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            client, out = Path(root) / "DSOClient", Path(root) / "server" / "data" / "client_unpacked"
            with patch.object(setup.subprocess, "run",
                              side_effect=lambda argv, **_: calls.append(argv) or type("R", (), {"returncode": 0})()):
                self.assertEqual(setup._unpack_client_bundles(client, out), 0)
        argv = calls[0]
        self.assertEqual(argv[argv.index("-o") + 1], str(out))
        self.assertEqual(argv[argv.index("--runtime-cache") + 1], str(out / "dlcache"))
        self.assertNotIn(str(client / "dlcache"), argv)


if __name__ == "__main__":
    unittest.main()

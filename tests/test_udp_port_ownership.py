"""An old Windows server must not share endpoints with a new instance."""

import os
import unittest

from dso_server.runtime.transport import RakNetServer


@unittest.skipUnless(os.name == "nt", "Windows UDP endpoint ownership")
class UdpPortOwnershipTests(unittest.TestCase):
    def test_second_server_cannot_share_live_udp_port(self):
        first = RakNetServer("127.0.0.1", 0, "port-owner-test")
        self.addCleanup(first.stop)
        first.start()
        port = first.sock.getsockname()[1]
        second = RakNetServer("127.0.0.1", port, "port-contender-test")
        self.addCleanup(second.stop)
        with self.assertRaises(OSError):
            second.start()
        self.assertIsNone(second.sock)
        self.assertEqual(first.sock.getsockname()[1], port)


if __name__ == "__main__":
    unittest.main()

"""Check home-network discovery when Flatpak has no iproute2 executable."""

from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discovery


class SandboxNetworkTests(unittest.TestCase):
    def network(self, address, mask):
        routes = ("Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
                  "eth0 00000000 0105000A 0003 0 0 100 00000000\n")
        replies = [b"\0" * 20 + socket.inet_aton(address), b"\0" * 20 + socket.inet_aton(mask)]
        with patch("discovery.shutil.which", return_value=None), \
                patch("discovery.Path.read_text", return_value=routes), \
                patch("fcntl.ioctl", side_effect=replies), \
                patch("discovery.subprocess.check_output", side_effect=AssertionError("Host command used")):
            return discovery.home_network()

    def test_no_host_executable_needed(self):
        network, own = self.network("10.0.5.9", "255.255.255.0")
        self.assertEqual(str(network), "10.0.5.0/24")
        self.assertEqual(own, "10.0.5.9")

    def test_large_network_is_bounded(self):
        network, _own = self.network("10.0.5.9", "255.255.0.0")
        self.assertEqual(str(network), "10.0.5.0/24")

    def test_smaller_subnet_is_preserved(self):
        network, _own = self.network("10.0.5.9", "255.255.255.128")
        self.assertEqual(str(network), "10.0.5.0/25")

    def test_public_addresses_are_rejected(self):
        with self.assertRaises(ValueError):
            self.network("203.0.113.9", "255.255.255.0")


if __name__ == "__main__":
    unittest.main()

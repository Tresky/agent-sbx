"""dns_has against a tiny local DNS responder: the header parse and the
controlled pair of an existing and a missing name."""
import socket
import struct
import threading
import unittest

from sbxlib.vm import dns_has


class FakeDns:
    """Answers with ANCOUNT=1 for names that contain 'yes', NXDOMAIN otherwise."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            data, peer = self.sock.recvfrom(512)
            qid, qname = data[:2], data[12:]
            if b"yes" in qname:
                head = qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
            else:
                head = qid + struct.pack(">HHHHH", 0x8183, 1, 0, 0, 0)  # rcode 3, NXDOMAIN
            self.sock.sendto(head + qname, peer)


class DnsHasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeDns()

    def test_controlled_pair(self):
        self.assertTrue(dns_has("127.0.0.1", "sbx-yes.sbx.internal", port=self.server.port))
        self.assertFalse(dns_has("127.0.0.1", "sbx-nope.sbx.internal", port=self.server.port))

    def test_no_server_is_false_not_an_exception(self):
        # A closed UDP port: the query times out or is refused; either is False.
        self.assertFalse(dns_has("127.0.0.1", "x.internal", timeout=0.3, port=9))


if __name__ == "__main__":
    unittest.main()

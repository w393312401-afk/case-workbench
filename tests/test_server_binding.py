"""A running workbench owns its loopback port; only temporary servers are used."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_server as fixture


class ServerBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-workbench-binding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.servers = []
        self.addCleanup(self.close_all)
        self.silence = patch.object(fixture.SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)

    def start_server(self, port=0):
        server = fixture.SERVER.make_server(self.root / "skill", self.root / "runtime", port=port, allow_open=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.servers.append((server, thread))
        thread.start()
        return server

    def close_server(self, server):
        entry = next(pair for pair in self.servers if pair[0] is server)
        server.shutdown()
        server.server_close()
        entry[1].join(timeout=3)
        self.assertFalse(entry[1].is_alive())
        self.servers.remove(entry)

    def close_all(self):
        for server, _ in list(self.servers):
            self.close_server(server)

    def state(self, server):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200, body)
            self.assertEqual(body["app"], "video-case-workbench")
            return body
        finally:
            connection.close()

    def test_second_workbench_on_same_port_fails_without_harming_first(self):
        first = self.start_server()
        before = self.state(first)
        self.assertEqual(first.server_address[0], "127.0.0.1")
        with self.assertRaises(OSError):
            fixture.SERVER.make_server(self.root / "other-skill", self.root / "other-runtime",
                                       port=first.server_port, allow_open=False)
        self.assertEqual(self.state(first)["token"], before["token"])

    def test_closed_port_can_be_rebound_and_served(self):
        first = self.start_server()
        old_token = self.state(first)["token"]
        port = first.server_port
        self.close_server(first)
        replacement = self.start_server(port)
        self.assertEqual(replacement.server_port, port)
        self.assertNotEqual(self.state(replacement)["token"], old_token)

    def test_ephemeral_servers_still_receive_separate_usable_ports(self):
        first = self.start_server()
        second = self.start_server()
        self.assertNotEqual(first.server_port, second.server_port)
        self.assertGreater(first.server_port, 0)
        self.assertGreater(second.server_port, 0)
        self.assertNotEqual(self.state(first)["token"], self.state(second)["token"])
        self.assertFalse(first.allow_reuse_port)
        self.assertEqual(first.allow_reuse_address, os.name != "nt")

    @unittest.skipUnless(os.name == "nt", "Windows exclusive socket option")
    def test_windows_listener_rejects_even_a_reuseaddr_socket(self):
        server = self.start_server()
        self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as challenger:
            challenger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                challenger.bind(server.server_address)
        self.state(server)


if __name__ == "__main__":
    unittest.main()

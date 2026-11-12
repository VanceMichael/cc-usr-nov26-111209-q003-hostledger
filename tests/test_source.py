import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from hostledger.source_server import FixtureServer
from hostledger.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_binary_delivery_not_hex_wrapper(self):
        from hostledger.parser import inspect_document
        body = self.get('/file')[2]
        self.assertFalse(body.startswith(b'HEX'))
        self.assertEqual(len(inspect_document(body)['records']), 4)

    def test_redelivery_has_different_bytes(self):
        self.assertNotEqual(self.get('/file')[2], self.get('/redelivery')[2])

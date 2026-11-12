import unittest
from pathlib import Path
from hostledger.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_frames_and_offsets(self):
        result = inspect_document(Path('hostledger/fixtures/sample.hex').read_bytes())
        self.assertEqual(len(result['records']), 4)
        self.assertEqual(result['records'][1]['offset'], result['records'][0]['length'])
        self.assertTrue(result['records'][0]['preview'].startswith('H01'))

    def test_binary_equals_hex(self):
        body = Path('hostledger/fixtures/sample.hex').read_bytes()
        self.assertEqual(inspect_document(body), inspect_document(bytes.fromhex(body[4:].decode())))

    def test_truncated(self):
        for body in [b'\x00', b'\x00\x07\x00\x00a']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                inspect_document(body)

    def test_reserved_and_small_length(self):
        for body in [b'\x00\x04\x01\x00', b'\x00\x03\x00\x00']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                inspect_document(body)

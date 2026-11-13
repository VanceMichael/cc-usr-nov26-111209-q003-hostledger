import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from builders import comp3, controlled, delivery, frame, header, plain, trailer  # noqa: E402

from hostledger.ledger import Ledger  # noqa: E402

FIXTURES = Path(__file__).parent.parent / 'hostledger' / 'fixtures'


def fixture(name):
    return (FIXTURES / name).read_bytes()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Ledger(Path(self.directory.name) / 'ledger.sqlite3',
                             Path(self.directory.name) / 'objects')
        self.addCleanup(self.ledger.close)

    def capture(self, name, url=None):
        return self.ledger.save(url or 'local:' + name, 200, 'application/octet-stream',
                                fixture(name))

    def test_receive_sample_exposes_each_item(self):
        receipt = self.ledger.receive(self.capture('sample.hex'), filename='sample.hex')
        self.assertEqual(self.ledger.receipt(receipt)['status'], 'accepted')
        items = {entry['item_id']: entry for entry in self.ledger.query(batch_no='B0001')}
        self.assertEqual(sorted(items), ['ITEM001', 'ITEM002'])
        self.assertEqual(items['ITEM001']['amount'], '12.34')
        self.assertEqual(items['ITEM001']['record_offset'], 12)
        self.assertEqual(items['ITEM001']['layout_version'], '01')
        self.assertEqual(items['ITEM001']['fields']['count']['start'], 27)
        self.assertEqual(items['ITEM002']['amount'], '-0.50')
        self.assertEqual(items['ITEM002']['temperature'], '005')

    def test_export_by_business_batch_keeps_controls(self):
        self.ledger.receive(self.capture('sample.hex'))
        exported = self.ledger.export_batch('B0001')
        self.assertEqual(exported['layout_version'], '01')
        self.assertEqual(exported['detail_count'], 2)
        self.assertEqual((exported['n_count'], exported['c_count']), (1, 1))
        self.assertEqual(exported['total'], '11.84')
        self.assertEqual(len(exported['entries']), 2)

    def test_isolated_receipt_is_reviewable_but_not_queryable(self):
        receipt = self.ledger.receive(self.capture('control-bad.hex'))
        receipt_row = self.ledger.receipt(receipt)
        self.assertEqual(receipt_row['status'], 'isolated')
        self.assertIn('头尾控制明细数不符', receipt_row['reason'])

        review = self.ledger.review(receipt)
        self.assertEqual(review['status'], 'isolated')
        self.assertEqual(len(review['entries']), 1)
        self.assertEqual(review['entries'][0]['record_hex'], 'd5c9e3c5d4f0f0f101234cf1c1c1')
        self.assertEqual(review['entries'][0]['record_offset'], 12)

        self.assertIsNone(self.ledger.export_batch('B0001'))
        self.assertEqual(self.ledger.query(item_id='ITEM001'), [])

        accepted = self.ledger.receive(self.capture('sample.hex'))
        with self.assertRaises(ValueError):
            self.ledger.review(accepted)
        self.assertEqual(self.ledger.export_batch('B0001')['receipt_id'], accepted)

    def test_half_batch_never_becomes_visible(self):
        control_bad = self.ledger.receive(self.capture('control-bad.hex'))
        self.assertEqual(self.ledger.receipts(), [self.receipt_summary(control_bad)])
        good = self.ledger.receive(self.capture('sample.hex'))
        self.assertEqual(len(self.ledger.query(batch_no='B0001')), 2)
        self.assertEqual(self.ledger.export_batch('B0001')['receipt_id'], good)

    def receipt_summary(self, receipt_id):
        return next(row for row in self.ledger.receipts() if row['id'] == receipt_id)

    def test_unknown_and_bad_decimal_keep_raw_span_for_operators(self):
        for name, expected in [('unknown.hex', '未知布局版本 99'),
                               ('bad-decimal.hex', '非法十进制半字节 A')]:
            with self.subTest(name=name):
                receipt = self.ledger.receive(self.capture(name), filename=name)
                review = self.ledger.review(receipt)
                self.assertEqual(review['status'], 'isolated')
                self.assertIn(expected, review['reason'] + (review['entries'][0]['reason'] or ''))
                self.assertEqual(review['entries'][0]['record_offset'], 12)
                self.assertEqual(review['entries'][0]['record_length'], 18)
                self.assertTrue(review['entries'][0]['record_hex'])

    def test_same_filename_and_content_creates_independent_receipts(self):
        first = self.ledger.receive(self.capture('sample.hex', 'local:first'), filename='sample.hex')
        second = self.ledger.receive(self.capture('sample.hex', 'local:second'), filename='sample.hex')
        self.assertNotEqual(first, second)
        first_row = self.ledger.receipt(first)
        second_row = self.ledger.receipt(second)
        self.assertIsNone(first_row['supersedes'])
        self.assertIsNone(second_row['supersedes'])
        self.assertEqual(first_row['digest'], second_row['digest'])
        self.assertEqual(len(list(Path(self.directory.name, 'objects').iterdir())), 1)
        self.assertEqual(len(self.ledger.receipts(batch_no='B0001')), 2)

    def test_redelivery_supersedes_but_keeps_original_bytes(self):
        original = self.ledger.receive(self.capture('sample.hex'), filename='sample.hex')
        replacement = self.ledger.receive(self.capture('redelivery.hex'), filename='sample.hex')
        first = self.ledger.receipt(original)
        second = self.ledger.receipt(replacement)
        self.assertEqual(first['supersedes'], None)
        self.assertEqual(second['supersedes'], original)
        original_bytes = (Path(self.directory.name) / 'objects' / first['digest']).read_bytes()
        self.assertEqual(original_bytes, fixture('sample.hex'))

        explanation = self.ledger.explain_redelivery('B0001')
        self.assertEqual([receipt['receipt_id'] for receipt in explanation['receipts']],
                         [original, replacement])
        self.assertEqual(explanation['receipts'][0]['item_ids'], ['ITEM001', 'ITEM002'])
        self.assertEqual(explanation['receipts'][1]['item_ids'], ['ITEM001'])
        self.assertEqual(explanation['current_receipt_id'], replacement)

        exported = self.ledger.export_batch('B0001')
        self.assertEqual(exported['receipt_id'], replacement)
        self.assertEqual([entry['item_id'] for entry in exported['entries']], ['ITEM001'])
        self.assertEqual(exported['total'], '12.34')
        self.assertEqual(len(self.ledger.query(item_id='ITEM001')), 1)
        self.assertEqual(self.ledger.query(item_id='ITEM002'), [])

    def test_isolated_redelivery_does_not_replace_accepted_batch(self):
        accepted = self.ledger.receive(self.capture('sample.hex'))
        rejected = self.ledger.receive(self.capture('control-bad.hex'))
        self.assertIsNone(self.ledger.receipt(rejected)['supersedes'])
        self.assertEqual(self.ledger.export_batch('B0001')['receipt_id'], accepted)
        explanation = self.ledger.explain_redelivery('B0001')
        self.assertEqual(explanation['current_receipt_id'], accepted)
        self.assertEqual([receipt['status'] for receipt in explanation['receipts']],
                         ['accepted', 'isolated'])

    def test_interrupted_write_rolls_back_and_keeps_original(self):
        capture = self.capture('sample.hex')
        receipt = self.ledger.receive(capture)
        original = self.ledger.receipt(receipt)

        redelivery = self.capture('redelivery.hex')
        with self.assertRaises(RuntimeError):
            self.ledger.receive(redelivery, filename='redelivery.hex', interrupt=0)
        self.assertEqual([row['id'] for row in self.ledger.receipts()], [receipt])
        self.assertEqual(self.ledger.export_batch('B0001')['receipt_id'], receipt)
        self.assertEqual(self.ledger.receipt(receipt), original)
        self.assertEqual(self.ledger.query(item_id='ITEM001')[0]['record_index'], 1)

        # 原文对象保持只读且仍可重新接收
        self.assertEqual(self.ledger.read(redelivery), fixture('redelivery.hex'))
        self.assertEqual(self.ledger.receipt(self.ledger.receive(redelivery))['status'],
                         'accepted')

    def test_interruption_in_the_middle_of_entries_leaves_no_partial_batch(self):
        with self.assertRaises(RuntimeError):
            self.ledger.receive(self.capture('sample.hex'), interrupt=1)
        self.assertEqual(self.ledger.receipts(), [])
        self.assertEqual(self.ledger.query(), [])
        self.assertIsNone(self.ledger.export_batch('B0001'))
        self.assertEqual(
            self.ledger.connection.execute('select count(*) from receipt_entries').fetchone()[0], 0)

    def test_category_counts_derive_from_record_types(self):
        body = delivery(plain(amount='1.00'), plain(item='ITEM003', amount='2.00'),
                        controlled(amount='3.00', temperature='004'),
                        count='000003', total='0000000600')
        capture = self.ledger.save('local:synthetic', 200, 'application/octet-stream', body)
        receipt = self.ledger.receive(capture, filename='synthetic.hex')
        row = self.ledger.receipts()[0]
        self.assertEqual((row['detail_count'], row['n_count'], row['c_count']), (3, 2, 1))
        self.assertEqual(row['total'], '6.00')
        self.assertEqual(row['status'], 'accepted')
        self.assertEqual(self.ledger.export_batch('B0001')['receipt_id'], receipt)

    def test_non_200_capture_is_archived_but_not_received(self):
        capture = self.ledger.save('local:missing', 404, 'application/json', b'{"error":"x"}')
        self.assertEqual(self.ledger.read(capture), b'{"error":"x"}')
        with self.assertRaises(ValueError):
            self.ledger.receive(capture)
        self.assertEqual(self.ledger.receipts(), [])

    def test_unknown_capture_cannot_be_received(self):
        with self.assertRaises(KeyError):
            self.ledger.receive(999)


class InterruptedObjectWriteTests(unittest.TestCase):
    def test_unreferenced_object_does_not_imply_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / 'ledger.sqlite3', Path(directory) / 'objects')
            try:
                body = fixture('sample.hex')
                ledger.save('local:orphan', 200, 'application/octet-stream', body)
                self.assertEqual(ledger.receipts(), [])
                self.assertIsNone(ledger.export_batch('B0001'))
                self.assertEqual(ledger.query(), [])
            finally:
                ledger.close()


if __name__ == '__main__':
    unittest.main()

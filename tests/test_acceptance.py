import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from builders import comp3, controlled, delivery, frame, header, plain, trailer  # noqa: E402

from hostledger.parser import decode_comp3, parse_delivery  # noqa: E402

FIXTURES = Path(__file__).parent.parent / 'hostledger' / 'fixtures'


def fixture(name):
    return (FIXTURES / name).read_bytes()


class AcceptanceTests(unittest.TestCase):
    def test_sample_batch_is_accepted_with_exact_controls(self):
        result = parse_delivery(fixture('sample.hex'))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['reasons'], [])
        self.assertEqual(result['batch_no'], 'B0001')
        self.assertEqual(result['layout_version'], '01')
        self.assertEqual(result['computed']['detail_count'], 2)
        self.assertEqual(result['computed']['n_count'], 1)
        self.assertEqual(result['computed']['c_count'], 1)
        self.assertEqual(result['computed']['total'], '11.84')
        self.assertEqual(result['control']['detail_count'], 2)
        self.assertEqual(result['control']['total'], '11.84')

    def test_each_entry_keeps_record_start_layout_and_byte_range(self):
        result = parse_delivery(fixture('sample.hex'))
        entry = result['entries'][0]
        self.assertEqual(entry['record_offset'], 12)
        self.assertEqual(entry['record_length'], 18)
        self.assertEqual(entry['layout_version'], '01')
        self.assertEqual(entry['item_id'], 'ITEM001')
        self.assertEqual(entry['fields']['id'], {'start': 17, 'end': 24})
        self.assertEqual(entry['fields']['amount'], {'start': 24, 'end': 27})
        self.assertEqual(entry['fields']['count']['start'], 27)
        self.assertEqual(entry['fields']['groups'], {'start': 28, 'end': 30})
        self.assertIn('fields', entry)

    def test_offsets_are_absolute_from_file_start(self):
        body = delivery(plain(amount='12.34'), controlled(), count='000001', total='000011.84'.replace('.', ''))
        result = parse_delivery(body)
        detail = result['entries'][0]
        self.assertEqual(body[detail['record_offset']:detail['record_offset'] + 4],
                         (detail['record_length']).to_bytes(2, 'big') + b'\x00\x00')
        self.assertEqual(body[detail['payload_offset']:detail['payload_offset'] + 8],
                         b'\xd5' + 'ITEM001'.encode('cp037'))

    def test_signed_comp3_negative_amount(self):
        result = parse_delivery(fixture('sample.hex'))
        entry = result['entries'][1]
        self.assertEqual(entry['amount'], '-0.50')
        self.assertEqual(entry['amount_sign'], -1)
        self.assertEqual(entry['amount_units'], 50)
        self.assertEqual(entry['temperature'], '005')
        self.assertTrue(entry['temperature_provided'])

    def test_comp3_accepts_c_f_and_negative_d(self):
        for sign, expected in [('C', '123.45'), ('F', '123.45'), ('D', '-123.45')]:
            with self.subTest(sign=sign):
                self.assertEqual(decode_comp3(bytes.fromhex('12345' + sign.lower()), 2)[0], expected)

    def test_non_digit_nibble_is_isolated(self):
        body = delivery(plain(amount='12.34'), count='000001', total='0000001234')
        broken = body.replace(b'\x01\x23\x4c', b'\x01\xa3\x4c')
        self.assertNotEqual(body, broken)
        result = parse_delivery(broken)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['entries'][0]['status'], 'isolated')
        self.assertIn('非法十进制半字节 A', result['entries'][0]['reason'])
        self.assertIsNone(result['entries'][0]['amount'])


class LayoutVersionTests(unittest.TestCase):
    def test_version_and_type_jointly_select_layout(self):
        body = delivery(plain(amount='123.456', count='2', groups=('AB', 'CD'), version='02'),
                        controlled(amount='1.234', version='02'),
                        version='02', count='000002', total='0000124690')
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'accepted', result['reason'])
        plain_entry, cold_entry = result['entries']
        self.assertEqual(plain_entry['layout_version'], '02')
        self.assertEqual(plain_entry['amount'], '123.456')
        self.assertEqual(plain_entry['count'], 2)
        self.assertEqual([group['text'] for group in plain_entry['groups']], ['AB', 'CD'])
        self.assertEqual(cold_entry['record_type'], 'C')
        self.assertEqual(cold_entry['amount'], '1.234')
        self.assertEqual(cold_entry['temperature'], '005')
        self.assertEqual(cold_entry['fields']['amount'], {'start': 45, 'end': 49})

    def test_unknown_version_isolates_without_guessing(self):
        result = parse_delivery(fixture('unknown.hex'))
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('未知布局版本 99', result['reason'])
        entry = result['entries'][0]
        self.assertEqual(entry['status'], 'isolated')
        self.assertIsNone(entry['item_id'])
        self.assertIsNone(entry['amount'])
        self.assertIsNone(entry['groups'])
        self.assertEqual(entry['record_hex'], result['entries'][0]['record_hex'])

    def test_cold_section_reuses_plain_byte_area_per_layout(self):
        body = delivery(controlled(version='02', temperature='003'), version='02',
                        count='000001', total='000000.50'.replace('.', ''))
        result = parse_delivery(body)
        entry = result['entries'][0]
        self.assertEqual(entry['temperature'], '003')
        self.assertEqual(entry['fields']['temperature']['start'], 28)
        self.assertEqual(entry['temperature_provided'], True)


class ControlTests(unittest.TestCase):
    def test_count_mismatch_isolates_whole_batch(self):
        result = parse_delivery(fixture('control-bad.hex'))
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('头尾控制明细数不符：声明 2，实得 1', result['reasons'])
        self.assertIn('头尾控制总额不符：声明 11.84，实得 12.34', result['reasons'])
        self.assertEqual(result['entries'][0]['status'], 'accepted')

    def test_bad_decimal_blocks_total_and_batch(self):
        result = parse_delivery(fixture('bad-decimal.hex'))
        self.assertEqual(result['status'], 'isolated')
        self.assertIsNone(result['computed']['total'])
        self.assertIn('存在无法解析的金额，未核对总额', result['reasons'])

    def test_precise_decimal_total_not_float(self):
        body = delivery(plain(amount='0.10'), plain(amount='0.20'),
                        count='000002', total='0000000030')
        result = parse_delivery(body)
        self.assertEqual(result['computed']['total'], '0.30')
        self.assertEqual(result['status'], 'accepted', result['reason'])

    def test_missing_or_misplaced_header_and_trailer(self):
        body = delivery(plain(amount='12.34'))
        for broken in [body[12:], frame(body[12:20] + body[20:])]:
            with self.subTest(broken=broken[:4]):
                result = parse_delivery(broken)
                self.assertEqual(result['status'], 'isolated')

    def test_nested_header_isolates(self):
        body = header() + plain(amount='12.34') + header() + trailer()
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('头尾之间出现 H 记录', result['reason'])

    def test_truncated_stream_reports_raw_span(self):
        body = delivery(plain(amount='12.34'))
        truncated = body[:-3]
        result = parse_delivery(truncated)
        self.assertEqual(result['status'], 'isolated')
        self.assertIsNotNone(result['framing_error'])
        self.assertTrue(result['framing_error']['remaining_hex'])


class BlankAndZeroTests(unittest.TestCase):
    def test_zero_group_count_is_accepted_with_zero_groups(self):
        body = delivery(plain(amount='12.34', count='0', groups=()))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'accepted', result['reason'])
        self.assertEqual(result['entries'][0]['count'], 0)
        self.assertEqual(result['entries'][0]['count_provided'], True)
        self.assertEqual(result['entries'][0]['groups'], [])

    def test_blank_count_is_isolated_not_zero(self):
        body = delivery(plain(amount='12.34', count_byte=b'\x40', groups=()))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('件数控制未提供', result['entries'][0]['reason'])
        self.assertIsNone(result['entries'][0]['count'])
        self.assertIs(result['entries'][0]['count_provided'], False)

    def test_blank_temperature_is_isolated_not_zero(self):
        body = delivery(controlled(temperature='   '))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('最低温度未提供', result['entries'][0]['reason'])
        self.assertIsNone(result['entries'][0]['temperature'])
        self.assertIs(result['entries'][0]['temperature_provided'], False)

    def test_zero_amount_and_zero_count_are_not_blank(self):
        body = delivery(plain(amount='0.00', count='0', groups=()), count='000001',
                        total='0000000000')
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'accepted', result['reason'])
        self.assertEqual(result['entries'][0]['amount'], '0.00')
        self.assertIs(result['entries'][0]['count_provided'], True)

    def test_zero_temperature_is_provided_zero(self):
        body = delivery(controlled(amount='0', temperature='000'),
                        count='000001', total='0000000000')
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'accepted', result['reason'])
        entry = result['entries'][0]
        self.assertEqual(entry['temperature'], '000')
        self.assertEqual(entry['temperature_value'], 0)
        self.assertIs(entry['temperature_provided'], True)
        self.assertEqual(entry['amount'], '0.00')

    def test_padding_space_is_recorded_and_non_space_is_isolated(self):
        spaced = frame(b'\xd5ITEM001' + comp3('12.34') + b'\xf1' + b'\xc1\xc1' + b'\x40')
        spaced = header() + spaced + trailer(count='000001', total='0000001234')
        result = parse_delivery(spaced)
        self.assertEqual(result['status'], 'accepted')
        detail = result['entries'][0]
        self.assertEqual(detail['group_padding']['hex'], '40')
        self.assertEqual(detail['group_padding']['start'], detail['fields']['groups']['end'])

        contaminated = frame(b'\xd5ITEM001' + comp3('12.34') + b'\xf1' + b'\xc1\xc1\x41')
        body = header() + contaminated + trailer(count='000001', total='0000001234')
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('重复组后填充非空格', result['entries'][0]['reason'])
        self.assertIsNotNone(result['entries'][0]['group_padding'])


class RepeatGroupBoundsTests(unittest.TestCase):
    def test_group_count_out_of_range(self):
        body = delivery(plain(amount='12.34', count='5', groups=('AA',)))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('件数控制 5 超出 0-4', result['entries'][0]['reason'])
        self.assertEqual(len(result['entries'][0]['groups']), 0)

    def test_groups_out_of_bounds(self):
        body = delivery(plain(amount='12.34', count='3', groups=('AA', 'BB')))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('重复组越界', result['entries'][0]['reason'])
        self.assertEqual(result['entries'][0]['groups'], [])

    def test_invalid_group_code_isolated(self):
        body = delivery(plain(amount='12.34', count='1', groups=('A\x00',)))
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('重复组第 1 组货类编码非法', result['entries'][0]['reason'])


class ShortRecordTests(unittest.TestCase):
    def test_required_field_outside_payload(self):
        short = frame(b'\xd5ITEM001' + comp3('12.34'))
        body = header() + short + trailer(count='000001', total='0000001234')
        result = parse_delivery(body)
        self.assertEqual(result['status'], 'isolated')
        self.assertIn('件数控制字段越界', result['entries'][0]['reason'])
        self.assertIsNone(result['entries'][0]['item_id'])


if __name__ == '__main__':
    unittest.main()

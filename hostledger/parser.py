import json
from decimal import Decimal
from pathlib import Path

LAYOUT_FILE = Path(__file__).parent / 'fixtures' / 'layouts.json'
TYPE_NAMES = {0xC8: 'H', 0xD5: 'N', 0xC3: 'C', 0xE3: 'T'}
SPACE = 0x40


def load_layouts(path=None):
    return json.loads(Path(path or LAYOUT_FILE).read_text())


def _stream(body):
    if body.startswith(b'HEX\n'):
        return bytes.fromhex(body[4:].decode('ascii'))
    return body


def split_frames(body):
    """把 RDW 流切成完整帧；结构错误时返回已切出的帧和原始跨度。"""
    frames, offset = [], 0
    while offset < len(body):
        remaining = len(body) - offset
        error = None
        if remaining < 4:
            error = f'RDW 头截断：偏移 {offset} 只剩 {remaining} 字节'
        else:
            length = int.from_bytes(body[offset:offset + 2], 'big')
            if length < 4:
                error = f'RDW 长度无效：偏移 {offset} 长度为 {length}'
            elif body[offset + 2:offset + 4] != b'\x00\x00':
                error = f'RDW 保留位非零：偏移 {offset}'
            elif offset + length > len(body):
                error = (f'RDW 正文截断：偏移 {offset} 需要到 {offset + length}，'
                         f'文件只有 {len(body)} 字节')
        if error:
            return frames, {'offset': offset, 'message': error,
                            'remaining_hex': body[offset:].hex()}
        stop = offset + length
        frames.append({
            'offset': offset,
            'length': length,
            'end': stop,
            'payload_offset': offset + 4,
            'payload_end': stop,
            'payload': body[offset + 4:stop],
        })
        offset = stop
    return frames, None


def inspect_document(body):
    body = _stream(body)
    frames, error = split_frames(body)
    if error:
        raise ValueError(error['message'])
    return {
        'size': len(body),
        'records': [
            {'offset': frame['offset'], 'length': frame['length'],
             'hex': frame['payload'].hex(), 'preview': frame['payload'].decode('cp037')}
            for frame in frames
        ],
    }


def _absolute(frame, start, length):
    return {'start': frame['payload_offset'] + start,
            'end': frame['payload_offset'] + start + length}


def _text(data):
    return data.decode('cp037')


def _printable(text):
    return all(0x20 <= ord(character) <= 0x7E for character in text)


def _joined(reasons):
    return '；'.join(reasons) or None


def _base_record(frame, index, record_type, type_byte):
    return {
        'record_index': index,
        'record_type': record_type,
        'record_type_byte': type_byte,
        'record_offset': frame['offset'],
        'record_length': frame['length'],
        'payload_offset': frame['payload_offset'],
        'payload_end': frame['payload_end'],
        'record_hex': frame['payload'].hex(),
        'fields': {},
        'status': 'accepted',
        'reason': None,
    }


def _parse_header(frame):
    payload, plen = frame['payload'], len(frame['payload'])
    header = _base_record(frame, 0, 'H', payload[0] if payload else None)
    header['layout_version'] = None
    header['batch_no'] = None
    reasons = []
    if not payload:
        reasons.append('文件头载荷为空')
    elif payload[0] != 0xC8:
        reasons.append('文件头类型字节不是 H')
    elif plen < 8:
        reasons.append('文件头必需字段越界')
    else:
        if plen > 8 and any(byte != SPACE for byte in payload[8:]):
            reasons.append('文件头额外字节非空格')
        version = _text(payload[1:3])
        batch_no = _text(payload[3:8])
        header['layout_version'] = version
        header['batch_no'] = batch_no
        header['fields'] = {
            'version': {'start': frame['payload_offset'] + 1,
                        'end': frame['payload_offset'] + 3, 'value': version},
            'batch': {'start': frame['payload_offset'] + 3,
                      'end': frame['payload_offset'] + 8, 'value': batch_no},
        }
        if not version.strip():
            reasons.append('布局版本为空')
        if not batch_no.strip():
            reasons.append('业务批次号为空')
    header['status'] = 'isolated' if reasons else 'accepted'
    header['reason'] = _joined(reasons)
    return header


def _signed_text_units(text):
    if text.isdigit():
        return int(text)
    if len(text) == 10 and text[0] in '+-' and text[1:].isdigit():
        return int(text[0] + text[1:])
    return None


def _parse_trailer(frame, index, scale):
    payload, plen = frame['payload'], len(frame['payload'])
    trailer = _base_record(frame, index, 'T', payload[0] if payload else None)
    trailer['detail_count'] = None
    trailer['total_units'] = None
    trailer['total'] = None
    trailer['total_scale'] = scale
    reasons = []
    if not payload:
        reasons.append('尾记录载荷为空')
    elif payload[0] != 0xE3:
        reasons.append('尾记录类型字节不是 T')
    elif plen < 17:
        reasons.append('尾记录必需字段越界')
    else:
        if plen > 17 and any(byte != SPACE for byte in payload[17:]):
            reasons.append('尾记录额外字节非空格')
        count_text = _text(payload[1:7])
        total_text = _text(payload[7:17])
        trailer['detail_count_text'] = count_text
        trailer['total_text'] = total_text
        trailer['fields'] = {
            'detail_count': {'start': frame['payload_offset'] + 1,
                             'end': frame['payload_offset'] + 7, 'value': count_text},
            'total': {'start': frame['payload_offset'] + 7,
                      'end': frame['payload_offset'] + 17, 'value': total_text},
        }
        if not count_text.isdigit():
            reasons.append('尾记录明细数不是六位文本数字')
        else:
            trailer['detail_count'] = int(count_text)
        total_units = _signed_text_units(total_text)
        if total_units is None:
            reasons.append('尾记录总额不是十位文本数字')
        elif scale is not None:
            trailer['total_units'] = total_units
            trailer['total'] = format(Decimal(total_units).scaleb(-scale), 'f')
    trailer['status'] = 'isolated' if reasons else 'accepted'
    trailer['reason'] = _joined(reasons)
    return trailer


def decode_comp3(data, scale):
    """解带符号 COMP-3，返回(精确十进制字符串, 原始数字, 符号)或错误原因。"""
    reasons = []
    if not data:
        return None, None, None, ['金额字段为空']
    sign_nibble = data[-1] & 0x0F
    if sign_nibble not in (0xC, 0xF, 0xD):
        reasons.append(f'非法十进制符号半字节 {sign_nibble:X}')
    digits = []
    for position, byte in enumerate(data):
        high, low = byte >> 4, byte & 0x0F
        if high > 9:
            reasons.append(f'非法十进制半字节 {high:X}（第 {position} 字节高半字节）')
        else:
            digits.append(str(high))
        if position != len(data) - 1:
            if low > 9:
                reasons.append(f'非法十进制半字节 {low:X}（第 {position} 字节低半字节）')
            else:
                digits.append(str(low))
    if reasons:
        return None, None, None, reasons
    units = int(''.join(digits)) if digits else 0
    sign = -1 if sign_nibble == 0xD else 1
    value = (Decimal(units) * sign).scaleb(-scale)
    return format(value, 'f'), units, sign, []


def _parse_temperature(data):
    if all(byte == SPACE for byte in data):
        return None, None, False, ['最低温度未提供']
    text = _text(data)
    if text.isdigit() or (len(text) == 3 and text[0] in '+-' and text[1:].isdigit()):
        return text, int(text), True, []
    return None, None, False, [f'温度字段非法：{text!r}']


def _parse_detail(frame, index, version, layouts, inherited_reason=None):
    payload, plen = frame['payload'], len(frame['payload'])
    type_byte = payload[0] if plen else None
    type_name = TYPE_NAMES.get(type_byte)
    entry = _base_record(frame, index, type_name, type_byte)
    entry['layout_version'] = version
    entry['item_id'] = None
    entry['amount'] = None
    entry['amount_units'] = None
    entry['amount_sign'] = None
    entry['amount_scale'] = None
    entry['count'] = None
    entry['count_provided'] = None
    entry['groups'] = None
    entry['group_padding'] = None
    entry['temperature'] = None
    entry['temperature_value'] = None
    entry['temperature_provided'] = None

    reasons = []
    if inherited_reason:
        reasons.append(inherited_reason)
    if type_byte is None:
        reasons.append('记录载荷为空')
    elif type_name not in ('N', 'C'):
        reasons.append(f'未知记录类型 {type_byte:02X}')
    if version is None:
        reasons.append('缺少可确定的布局版本')
    elif version not in layouts:
        reasons.append(f'未知布局版本 {version}')
    elif type_name in ('N', 'C') and type_name not in layouts[version]:
        reasons.append(f'布局 {version} 未定义记录类型 {type_name}')

    layout = layouts.get(version, {}).get(type_name) if version in layouts else None
    if layout is not None:
        required = [('id', '货票号', layout.get('id')),
                    ('amount', '金额', layout.get('amount'))]
        if type_name == 'N':
            required.extend([('count', '件数控制', layout.get('count')),
                             ('groups', '重复组', layout.get('groups'))])
        else:
            required.append(('temperature', '最低温度', layout.get('minimum_temperature')))
        bounds = []
        for key, label, field in required:
            if field is None:
                bounds.append(f'布局缺少{label}字段定义')
                continue
            entry['fields'][key] = {'start': _absolute(frame, field[0], field[1])['start'],
                                    'end': _absolute(frame, field[0], field[1])['end']}
            # 重复组的实际长度由件数控制决定，这里只要求组区起点在载荷内；
            # 声明组数越界时在读取阶段单独说明。
            if key == 'groups':
                if field[0] > plen:
                    bounds.append(f'{label}字段越界')
            elif field[0] + field[1] > plen:
                bounds.append(f'{label}字段越界')
        if bounds:
            reasons.extend(bounds)
            reasons.append('字段越界，未读取字段值')
        else:
            reasons.extend(_read_layout_fields(frame, entry, payload, plen, layout, type_name))

    entry['status'] = 'isolated' if reasons else 'accepted'
    entry['reason'] = _joined(reasons)
    return entry


def _read_layout_fields(frame, entry, payload, plen, layout, type_name):
    reasons = []
    item_field = layout['id']
    entry['item_id'] = _text(payload[item_field[0]:item_field[0] + item_field[1]])
    if not entry['item_id'].strip():
        reasons.append('货票号为空')

    amount_field = layout['amount']
    data = payload[amount_field[0]:amount_field[0] + amount_field[1]]
    amount, units, sign, amount_reasons = decode_comp3(data, amount_field[2])
    reasons.extend(amount_reasons)
    entry['amount'] = amount
    entry['amount_units'] = units
    entry['amount_sign'] = sign
    entry['amount_scale'] = amount_field[2]

    if type_name == 'N':
        count_start, count_length = layout['count']
        count_raw = payload[count_start:count_start + count_length]
        entry['fields']['count']['hex'] = count_raw.hex()
        if count_length != 1:
            reasons.append('件数字段宽度不是一字节')
        else:
            byte = count_raw[0]
            if byte == SPACE:
                entry['count_provided'] = False
                reasons.append('件数控制未提供')
            elif 0xF0 <= byte <= 0xF9:
                entry['count'] = byte - 0xF0
                entry['count_provided'] = True
                if entry['count'] > 4:
                    reasons.append(f'件数控制 {entry["count"]} 超出 0-4')
            else:
                entry['count_provided'] = False
                reasons.append('件数控制不是 cp037 数字')

        entry['groups'] = []
        group_start, group_width = layout['groups']
        if entry['count'] is not None:
            needed = entry['count'] * group_width
            group_field_end = frame['payload_offset'] + group_start + needed
            entry['fields']['groups'] = {'start': _absolute(frame, group_start, 0)['start'],
                                         'end': group_field_end}
            if group_start + needed > plen:
                reasons.append(
                    f'重复组越界：声明 {entry["count"]} 组需要到偏移 {group_start + needed}，'
                    f'载荷只有 {plen} 字节')
            else:
                for group_index in range(entry['count']):
                    relative = group_start + group_index * group_width
                    raw = payload[relative:relative + group_width]
                    text = _text(raw)
                    group = {
                        'index': group_index,
                        'text': text,
                        'hex': raw.hex(),
                        'start': _absolute(frame, relative, group_width)['start'],
                        'end': _absolute(frame, relative, group_width)['end'],
                    }
                    entry['groups'].append(group)
                    if not _printable(text):
                        reasons.append(f'重复组第 {group_index + 1} 组货类编码非法')
                padding_start = group_start + needed
                if padding_start < plen:
                    padding = payload[padding_start:]
                    entry['group_padding'] = {
                        'start': _absolute(frame, padding_start, len(padding))['start'],
                        'end': _absolute(frame, padding_start, len(padding))['end'],
                        'hex': padding.hex(),
                    }
                    if any(byte != SPACE for byte in padding):
                        reasons.append('重复组后填充非空格')
    else:
        field = layout['minimum_temperature']
        data = payload[field[0]:field[0] + field[1]]
        text, value, provided, temperature_reasons = _parse_temperature(data)
        reasons.extend(temperature_reasons)
        entry['temperature'] = text
        entry['temperature_value'] = value
        entry['temperature_provided'] = provided
        entry['fields']['temperature']['hex'] = data.hex()
    return reasons


def parse_delivery(body, layouts=None):
    """按文件头版本和记录类型联合解析一批 RDW，并核对头尾控制数。"""
    layouts = layouts or load_layouts()
    stream = _stream(body)
    frames, framing_error = split_frames(stream)
    reasons = []
    if framing_error:
        reasons.append(framing_error['message'])
    if not frames and not framing_error:
        reasons.append('文件为空')

    names = [TYPE_NAMES.get(frame['payload'][0]) if frame['payload'] else None
             for frame in frames]

    header_index = 0 if names and names[0] == 'H' else None
    if header_index is None:
        reasons.append('文件头 H 缺失或不在首记录')
    header = _parse_header(frames[header_index]) if header_index is not None else None
    if header is not None and header['reason']:
        reasons.append(header['reason'])

    trailer_index = len(frames) - 1 if names and names[-1] == 'T' else None
    if trailer_index is None:
        reasons.append('文件尾 T 缺失或不在末记录')

    for index, name in enumerate(names):
        if name in ('H', 'T') and index not in (header_index, trailer_index):
            reasons.append(f'头尾之间出现 {name} 记录（记录序 {index}）')

    version = header['layout_version'] if header else None
    batch_no = header['batch_no'] if header else None
    if version is not None and version.strip() and version not in layouts:
        reasons.append(f'未知布局版本 {version}')

    scale = layouts[version]['N']['amount'][2] if version in layouts else None
    trailer = _parse_trailer(frames[trailer_index], trailer_index, scale) \
        if trailer_index is not None else None
    if trailer is not None and trailer['reason']:
        reasons.append(trailer['reason'])

    detail_indices = [index for index, name in enumerate(names)
                      if index not in (header_index, trailer_index) and name not in ('H', 'T')]
    inherited = None if header is not None else '结构无效：缺少文件头'
    entries = [_parse_detail(frames[index], index, version, layouts, inherited)
               for index in detail_indices]

    n_count = sum(1 for entry in entries if entry['record_type'] == 'N')
    c_count = sum(1 for entry in entries if entry['record_type'] == 'C')
    unknown_count = sum(1 for entry in entries if entry['record_type'] not in ('N', 'C'))
    computed_count = n_count + c_count

    isolated = [entry for entry in entries if entry['status'] == 'isolated']
    if isolated:
        reasons.append(f'存在 {len(isolated)} 条隔离明细')
    if unknown_count:
        reasons.append(f'存在 {unknown_count} 条未知记录类型')

    control_count = trailer['detail_count'] if trailer else None
    control_total = trailer['total'] if trailer else None
    control_units = trailer['total_units'] if trailer else None
    if control_count is not None and control_count != computed_count:
        reasons.append(f'头尾控制明细数不符：声明 {control_count}，实得 {computed_count}')
    if computed_count != len(entries):
        reasons.append(f'分类型记录数不符：N+C 为 {computed_count}，明细序共 {len(entries)} 条')

    amounts_ok = scale is not None and all(
        entry['amount_units'] is not None for entry in entries
        if entry['record_type'] in ('N', 'C'))
    computed_total = None
    computed_units = None
    if amounts_ok and unknown_count == 0:
        computed_units = sum(entry['amount_sign'] * entry['amount_units'] for entry in entries)
        computed_total = format(Decimal(computed_units).scaleb(-scale), 'f')
        if control_units is not None and computed_units != control_units:
            reasons.append(f'头尾控制总额不符：声明 {control_total}，实得 {computed_total}')
    elif entries and scale is not None:
        reasons.append('存在无法解析的金额，未核对总额')
    if unknown_count:
        reasons.append('存在未知记录类型，未核对总额')

    return {
        'size': len(stream),
        'status': 'isolated' if reasons else 'accepted',
        'reasons': reasons,
        'reason': _joined(reasons),
        'batch_no': batch_no,
        'layout_version': version,
        'framing_error': framing_error,
        'header': header,
        'trailer': trailer,
        'control': {
            'detail_count': control_count,
            'total': control_total,
            'total_scale': scale,
        },
        'computed': {
            'detail_count': computed_count,
            'n_count': n_count,
            'c_count': c_count,
            'unknown_count': unknown_count,
            'total': computed_total,
            'total_units': computed_units,
            'total_scale': scale,
        },
        'entries': entries,
    }

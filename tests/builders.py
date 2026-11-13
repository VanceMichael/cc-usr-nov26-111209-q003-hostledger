"""本地合成的 RDW 装载明细构造器，仅用于测试。"""

COMP3_DIGITS = {'01': 5, '02': 7}


def frame(payload):
    return (len(payload) + 4).to_bytes(2, 'big') + b'\x00\x00' + payload


def digits(text):
    """把只含 0-9 的文本编码为 cp037 数字。"""
    return text.encode('cp037')


def comp3(amount, version='01'):
    """把金额文本按布局版式编码成带符号 COMP-3，末半字节 C 正、D 负。"""
    negative = amount.startswith('-')
    raw_digits = amount.lstrip('-').replace('.', '')
    width = COMP3_DIGITS[version]
    if len(raw_digits) > width:
        raise ValueError(f'版本 {version} 只容纳 {width} 位数字，得到 {len(raw_digits)}')
    padded = raw_digits.rjust(width, '0')
    return bytes.fromhex(padded + ('d' if negative else 'c'))


def header(version='01', batch='B0001'):
    return frame(b'\xc8' + digits(version) + batch.encode('cp037'))


def plain(item='ITEM001', amount='1234', count='1', groups=('AA',), version='01',
          count_byte=None):
    """N 记录：版本 01 count 在偏移 11、组从 12 起；版本 02 count 在 12、组从 13 起。"""
    count_field = digits(count) if count_byte is None else count_byte
    raw = b'\xd5' + item.encode('cp037') + comp3(amount, version) + count_field
    raw += b''.join(group.encode('cp037') for group in groups)
    return frame(raw)


def controlled(item='ITEM002', amount='500', version='01', temperature='005'):
    """C 记录：温度复用普通段的字节区域，版本 01 偏移 11 起，版本 02 偏移 12 起。"""
    raw = b'\xc3' + item.encode('cp037') + comp3(amount, version) + digits(temperature)
    return frame(raw)


def trailer(count='000001', total='0000001234'):
    return frame(b'\xe3' + digits(count) + digits(total))


def delivery(*records, version='01', batch='B0001', count='000001', total='0000001234'):
    return header(version, batch) + b''.join(records) + trailer(count, total)

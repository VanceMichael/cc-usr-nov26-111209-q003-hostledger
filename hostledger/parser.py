def inspect_document(body):
    if body.startswith(b'HEX\n'):
        body = bytes.fromhex(body[4:].decode('ascii'))
    offset, records = 0, []
    while offset < len(body):
        if len(body) - offset < 4:
            raise ValueError('RDW 头截断')
        length = int.from_bytes(body[offset:offset + 2], 'big')
        if length < 4 or body[offset + 2:offset + 4] != b'\x00\x00':
            raise ValueError('RDW 长度或保留位无效')
        stop = offset + length
        if stop > len(body):
            raise ValueError('RDW 正文截断')
        payload = body[offset + 4:stop]
        records.append({'offset': offset, 'length': length, 'hex': payload.hex(),
                        'preview': payload.decode('cp037')})
        offset = stop
    return {'size': len(body), 'records': records}

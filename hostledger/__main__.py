DESCRIPTION = '物流主机交付文件的 RDW 帧检查、接收台账及 EBCDIC 预览工具。'
DEMO_PATH = '/file'
DEMO_REDELIVERY_PATH = '/redelivery'

import argparse
import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .ledger import Ledger
from .parser import inspect_document, parse_delivery
from .source_server import FixtureServer
from .transport import fetch


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _repository(arguments, ledger=False):
    factory = Ledger if ledger else Archive
    return factory(arguments.db, arguments.objects)


def _with_repository(arguments, callback, ledger=False):
    repository = _repository(arguments, ledger)
    try:
        return callback(repository)
    finally:
        repository.close()


def _receive_url(arguments, ledger):
    status, content_type, body = fetch(arguments.url)
    capture_id = ledger.save(arguments.url, status, content_type, body)
    if status != 200:
        return {'capture_id': capture_id, 'http_status': status,
                'received': False, 'reason': f'HTTP 状态 {status}，未进入接收作业'}
    receipt_id = ledger.receive(capture_id, filename=arguments.filename)
    return {'capture_id': capture_id, 'http_status': status, 'receipt_id': receipt_id,
            'status': ledger.receipt(receipt_id)['status']}


def _add_repository_options(command):
    command.add_argument('--db', default='ledger.sqlite3')
    command.add_argument('--objects', default='objects')


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)

    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    inspect.add_argument('--full', action='store_true',
                         help='同时给出逐票解析结果（不写入台账）')

    capture = commands.add_parser('capture')
    capture.add_argument('url')
    _add_repository_options(capture)
    capture.add_argument('--filename')

    receive = commands.add_parser('receive')
    receive.add_argument('path', nargs='?')
    receive.add_argument('--url')
    receive.add_argument('--filename')
    _add_repository_options(receive)

    show = commands.add_parser('show')
    _add_repository_options(show)
    show.add_argument('--batch')

    review = commands.add_parser('review')
    review.add_argument('receipt_id', type=int)
    _add_repository_options(review)

    explain = commands.add_parser('explain')
    explain.add_argument('batch_no')
    _add_repository_options(explain)

    items = commands.add_parser('items')
    items.add_argument('--item')
    items.add_argument('--batch')
    _add_repository_options(items)

    export = commands.add_parser('export')
    export.add_argument('batch_no')
    export.add_argument('--output')
    _add_repository_options(export)

    commands.add_parser('demo')
    arguments = parser.parse_args()

    if arguments.command == 'inspect':
        body = Path(arguments.path).read_bytes()
        result = inspect_document(body)
        if arguments.full:
            result['delivery'] = parse_delivery(body)
    elif arguments.command == 'capture':
        result = _with_repository(arguments, lambda archive: _capture_only(arguments, archive))
    elif arguments.command == 'receive':
        if arguments.path and arguments.url:
            raise SystemExit('receive 一次只接受一个来源：本地路径或 --url')
        if arguments.path:
            if arguments.filename:
                raise SystemExit('--filename 只用于 HTTP 接收；本地文件沿用原文件名')
            body = Path(arguments.path).read_bytes()
            result = _with_repository(arguments, lambda ledger: _receive_bytes(
                ledger, arguments, body), ledger=True)
        elif arguments.url:
            result = _with_repository(arguments, lambda ledger: _receive_url(
                arguments, ledger), ledger=True)
        else:
            raise SystemExit('receive 需要本地路径或 --url')
    elif arguments.command == 'show':
        result = _with_repository(
            arguments, lambda ledger: ledger.receipts(arguments.batch), ledger=True)
    elif arguments.command == 'review':
        try:
            result = _with_repository(
                arguments, lambda ledger: ledger.review(arguments.receipt_id), ledger=True)
        except KeyError:
            raise SystemExit(f'接收事实 {arguments.receipt_id} 不存在')
        except ValueError as error:
            raise SystemExit(str(error))
    elif arguments.command == 'explain':
        result = _with_repository(
            arguments, lambda ledger: ledger.explain_redelivery(arguments.batch_no),
            ledger=True)
    elif arguments.command == 'items':
        result = _with_repository(
            arguments, lambda ledger: ledger.query(arguments.item, arguments.batch),
            ledger=True)
    elif arguments.command == 'export':
        result = _with_repository(
            arguments, lambda ledger: ledger.export_batch(arguments.batch_no), ledger=True)
        if result is None:
            raise SystemExit(f'批次 {arguments.batch_no} 没有完整接收的版本可导出')
        if arguments.output:
            Path(arguments.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2))
            result = {'batch_no': arguments.batch_no, 'output': arguments.output,
                      'detail_count': result['detail_count']}
    else:
        arguments.filename = None
        with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = 'http://127.0.0.1:' + str(server.server_address[1])
                ledger = Ledger(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
                try:
                    result = {'observations': []}
                    for path in (DEMO_PATH, DEMO_REDELIVERY_PATH):
                        arguments.url = base + path
                        result['observations'].append(_receive_url(arguments, ledger))
                    result['receipts'] = ledger.receipts()
                    explanation = ledger.explain_redelivery('B0001')
                    result['redelivery'] = explanation
                    result['export'] = ledger.export_batch('B0001')
                finally:
                    ledger.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
    _print(result)


def _capture_only(arguments, archive):
    status, content_type, body = fetch(arguments.url)
    return {'capture_id': archive.save(arguments.url, status, content_type, body),
            'http_status': status}


def _receive_bytes(ledger, arguments, body):
    capture_id = ledger.save('local:' + (arguments.filename or arguments.path), 200,
                             'application/octet-stream', body)
    receipt_id = ledger.receive(capture_id, filename=arguments.filename or
                                Path(arguments.path).name)
    return {'capture_id': capture_id, 'receipt_id': receipt_id,
            'status': ledger.receipt(receipt_id)['status']}


if __name__ == '__main__':
    main()

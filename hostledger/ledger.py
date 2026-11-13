import hashlib
import json
import sqlite3
from pathlib import Path

from .archive import Archive
from .parser import load_layouts, parse_delivery


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _flag(value):
    return None if value is None else 1 if value else 0


def _text(value):
    return None if value is None else str(value)


class Ledger(Archive):
    """在不可变原文之上记录逐次接收事实和逐票明细。

    原文仍由 Archive 按摘要保存；每次 receive 都追加一条接收事实，
    同名文件或重复内容不会被合并或跳过。整次接收在单个事务内写入，
    中断时数据库回滚，只可能多一份未被引用的原文对象，不会出现半个批次。
    """

    def __init__(self, database, objects, layouts=None):
        super().__init__(database, objects)
        self.layouts = layouts or load_layouts()
        self.connection.executescript("""
            create table if not exists receipts (
                id integer primary key,
                capture_id integer not null references captures(id),
                digest text not null,
                url text not null,
                filename text,
                received_at text not null default current_timestamp,
                supersedes integer references receipts(id),
                status text not null,
                reason text,
                reasons_json text not null,
                batch_no text,
                layout_version text,
                detail_count integer,
                n_count integer,
                c_count integer,
                unknown_count integer,
                rejected_count integer,
                total text,
                total_units integer,
                total_scale integer,
                header_json text,
                trailer_json text,
                control_json text,
                computed_json text,
                framing_json text
            );
            create index if not exists receipt_batch on receipts(batch_no);
            create index if not exists receipt_digest on receipts(digest);

            create table if not exists receipt_entries (
                receipt_id integer not null references receipts(id),
                record_index integer not null,
                record_type text,
                record_type_byte integer,
                layout_version text,
                record_offset integer not null,
                record_length integer not null,
                payload_offset integer not null,
                payload_end integer not null,
                record_hex text not null,
                item_id text,
                status text not null,
                reason text,
                amount text,
                amount_units integer,
                amount_sign integer,
                amount_scale integer,
                count integer,
                count_provided integer,
                temperature text,
                temperature_value integer,
                temperature_provided integer,
                fields_json text not null,
                groups_json text,
                group_padding_json text,
                primary key(receipt_id, record_index)
            );
            create index if not exists entry_item on receipt_entries(item_id);
            create index if not exists entry_status on receipt_entries(receipt_id, status);

            create table if not exists batch_announcements (
                receipt_id integer primary key references receipts(id)
            );
        """)

    def receive(self, capture_id, filename=None, layouts=None, interrupt=None):
        """解析一份已归档原文并追加接收事实。整次写入在单个事务内完成。

        interrupt 仅供本地故障演练：它在写入中途抛出异常，用于确认事务回滚、
        原文对象保持只读，并且不会留下半个批次。
        """
        body = self.read(capture_id)
        result = parse_delivery(body, layouts or self.layouts)
        digest = hashlib.sha256(body).hexdigest()
        capture = self.connection.execute(
            'select url, status from captures where id=?', (capture_id,)).fetchone()
        if capture is None:
            raise KeyError(capture_id)
        # 原文归档不等于业务接收完成：非 200 响应只保留取证事实。
        if capture[1] != 200:
            raise ValueError(f'HTTP 状态 {capture[1]}，原文已归档但未进入接收作业')
        if digest != self.connection.execute(
                'select digest from captures where id=?', (capture_id,)).fetchone()[0]:
            raise ValueError('原文对象与归档摘要不一致')

        supersedes = None
        if result['batch_no'] is not None and result['status'] == 'accepted':
            previous = self.connection.execute(
                'select id, digest from receipts where batch_no=? and status=? '
                'order by id desc limit 1',
                (result['batch_no'], 'accepted')).fetchone()
            if previous is not None and previous[1] != digest:
                supersedes = previous[0]

        computed = result['computed']
        rejected_count = sum(1 for entry in result['entries']
                             if entry['status'] != 'accepted')
        header = dict(result['header'] or {})
        trailer = dict(result['trailer'] or {})
        for record in (header, trailer):
            record.pop('fields', None)

        with self.connection:
            cursor = self.connection.execute(
                'insert into receipts(capture_id,digest,url,filename,supersedes,status,reason,'
                'reasons_json,batch_no,layout_version,detail_count,n_count,c_count,unknown_count,'
                'rejected_count,total,total_units,total_scale,header_json,'
                'trailer_json,control_json,computed_json,framing_json) '
                'values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (capture_id, digest, capture[0], filename, supersedes, result['status'],
                 result['reason'], _dump(result['reasons']), result['batch_no'],
                 result['layout_version'], computed['detail_count'], computed['n_count'],
                 computed['c_count'], computed['unknown_count'], rejected_count,
                 computed['total'], computed['total_units'], computed['total_scale'],
                 _dump(header), _dump(trailer) if trailer else None,
                 _dump(result['control']), _dump(computed),
                 _dump(result['framing_error'])))
            receipt_id = cursor.lastrowid
            for position, entry in enumerate(result['entries']):
                if interrupt is not None and position == interrupt:
                    raise RuntimeError(f'本地演练：第 {position} 条明细写入前中断')
                self.connection.execute(
                    'insert into receipt_entries(receipt_id,record_index,record_type,'
                    'record_type_byte,layout_version,record_offset,record_length,payload_offset,'
                    'payload_end,record_hex,item_id,status,reason,amount,amount_units,'
                    'amount_sign,amount_scale,count,count_provided,temperature,'
                    'temperature_value,temperature_provided,fields_json,groups_json,group_padding_json) '
                    'values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt_id, entry['record_index'], entry['record_type'],
                     entry['record_type_byte'], entry['layout_version'],
                     entry['record_offset'], entry['record_length'], entry['payload_offset'],
                     entry['payload_end'], entry['record_hex'], entry['item_id'],
                     entry['status'], entry['reason'], entry['amount'], entry['amount_units'],
                     entry['amount_sign'], entry['amount_scale'], entry['count'],
                     _flag(entry['count_provided']), entry['temperature'],
                     entry['temperature_value'], _flag(entry['temperature_provided']),
                     _dump(entry['fields']), _dump(entry['groups']),
                     _dump(entry['group_padding'])))
            if result['status'] == 'accepted' and result['batch_no'] is not None:
                self.connection.execute(
                    'insert into batch_announcements(receipt_id) values(?)', (receipt_id,))
        return receipt_id

    # ---- 查询与导出 -------------------------------------------------------

    def _row(self, row):
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def current_receipt(self, batch_no):
        """该批次号最近一次已接收的接收事实编号，没有则返回 None。"""
        row = self.connection.execute(
            'select receipt_id from batch_announcements join receipts '
            'on receipts.id = batch_announcements.receipt_id '
            'where receipts.batch_no=? order by receipts.id desc limit 1',
            (batch_no,)).fetchone()
        return None if row is None else row[0]

    def receipts(self, batch_no=None):
        """按接收顺序列出接收事实，包含被隔离的批次。"""
        self.connection.row_factory = sqlite3.Row
        sql = ('select id,capture_id,filename,received_at,status,reason,batch_no,'
               'layout_version,detail_count,n_count,c_count,unknown_count,rejected_count,'
               'total,total_units,total_scale,supersedes,digest from receipts')
        parameters = ()
        if batch_no is not None:
            sql += ' where batch_no=?'
            parameters = (batch_no,)
        sql += ' order by id'
        return [self._row(row) for row in self.connection.execute(sql, parameters)]

    def receipt(self, receipt_id):
        """返回一次接收事实及其全部逐票记录；隔离记录也在其中，带原因。"""
        self.connection.row_factory = sqlite3.Row
        receipt = self._row(self.connection.execute(
            'select * from receipts where id=?', (receipt_id,)).fetchone())
        if receipt is None:
            raise KeyError(receipt_id)
        for key in ('reasons_json', 'header_json', 'trailer_json', 'control_json',
                    'computed_json', 'framing_json'):
            receipt[key.removesuffix('_json')] = json.loads(receipt.pop(key))
        receipt['entries'] = [self._entry(row) for row in self.connection.execute(
            'select * from receipt_entries where receipt_id=? order by record_index',
            (receipt_id,))]
        return receipt

    def _entry(self, row):
        entry = self._row(row)
        for key in ('fields_json', 'groups_json', 'group_padding_json'):
                        entry[key.removesuffix('_json')] = json.loads(entry.pop(key))
        for key in ('count_provided', 'temperature_provided'):
            entry[key] = None if entry[key] is None else bool(entry[key])
        return entry

    def review(self, receipt_id):
        """只供隔离复核：列出记录及其隔离原因，不产生可用于正式查询的结果。"""
        receipt = self.receipt(receipt_id)
        if receipt['status'] == 'accepted':
            raise ValueError('已接收批次不进入隔离复核')
        return {
            'receipt_id': receipt_id,
            'batch_no': receipt['batch_no'],
            'status': receipt['status'],
            'reason': receipt['reason'],
            'reasons': receipt['reasons'],
            'entries': [
                {'record_index': entry['record_index'],
                 'record_type': entry['record_type'],
                 'layout_version': entry['layout_version'],
                 'record_offset': entry['record_offset'],
                 'record_length': entry['record_length'],
                 'record_hex': entry['record_hex'],
                 'item_id': entry['item_id'],
                 'status': entry['status'],
                 'reason': entry['reason']}
                for entry in receipt['entries']],
        }

    def current_batch(self, batch_no):
        """返回该业务批次号下最新一次已接收的完整批次；被隔离的接收不可见。"""
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            'select receipt_id from batch_announcements join receipts '
            'on receipts.id = batch_announcements.receipt_id '
            'where receipts.batch_no=? order by receipts.id desc limit 1', (batch_no,)).fetchone()
        return None if row is None else self.receipt(row[0])

    def export_batch(self, batch_no):
        """按业务批次导出最近一次接收的完整记录；没有完整接收时返回 None。"""
        receipt = self.current_batch(batch_no)
        if receipt is None:
            return None
        entries = [entry for entry in receipt['entries'] if entry['status'] == 'accepted']
        if len(entries) != len(receipt['entries']):
            raise ValueError('批次内存在未接收记录，不能导出')
        return {
            'batch_no': batch_no,
            'receipt_id': receipt['id'],
            'layout_version': receipt['layout_version'],
            'detail_count': receipt['detail_count'],
            'n_count': receipt['n_count'],
            'c_count': receipt['c_count'],
            'total': receipt['total'],
            'total_scale': receipt['total_scale'],
            'entries': entries,
        }

    def query(self, item_id=None, batch_no=None):
        """按票查询：只返回来自完整已接收批次的记录。"""
        self.connection.row_factory = sqlite3.Row
        # 只查每个业务批次最近一次完整接收；补发前的快照留给 explain 说明。
        sql = ('with current_receipt as ('
               ' select receipts.batch_no as batch_no, max(receipts.id) as id'
               ' from batch_announcements join receipts'
               ' on receipts.id = batch_announcements.receipt_id'
               ' group by receipts.batch_no)'
               ' select receipt_entries.*, receipts.batch_no as receipt_batch,'
               ' receipts.id as receipt_id from receipt_entries'
               ' join current_receipt on current_receipt.id = receipt_entries.receipt_id'
               ' join receipts on receipts.id = receipt_entries.receipt_id'
               ' where receipt_entries.status = ?')
        parameters = ['accepted']
        if item_id is not None:
            sql += ' and receipt_entries.item_id = ?'
            parameters.append(item_id)
        if batch_no is not None:
            sql += ' and receipts.batch_no = ?'
            parameters.append(batch_no)
        sql += ' order by receipts.id, receipt_entries.record_index'
        return [self._entry(row) for row in self.connection.execute(sql, parameters)]

    def explain_redelivery(self, batch_no):
        """说明该批次号历次接收事实，以及补发前后各自含哪些记录。"""
        receipts = self.receipts(batch_no)
        explanation = []
        for receipt in receipts:
            entries = self.receipt(receipt['id'])['entries']
            explanation.append({
                'receipt_id': receipt['id'],
                'received_at': receipt['received_at'],
                'filename': receipt['filename'],
                'digest': receipt['digest'],
                'status': receipt['status'],
                'reason': receipt['reason'],
                'supersedes': receipt['supersedes'],
                'item_ids': [entry['item_id'] for entry in entries],
                'accepted_item_ids': [entry['item_id'] for entry in entries
                                      if entry['status'] == 'accepted'],
                'isolated_record_indices': [entry['record_index'] for entry in entries
                                            if entry['status'] != 'accepted'],
            })
        return {
            'batch_no': batch_no,
            'current_receipt_id': self.current_receipt(batch_no),
            'receipts': explanation,
        }

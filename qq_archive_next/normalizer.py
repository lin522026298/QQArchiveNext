"""Reuse the existing QQ message interpretation; always retain the native row."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

vendor = Path(__file__).resolve().parents[1] / 'vendor/nt_msg_db_util'
if vendor.is_dir() and str(vendor) not in sys.path:
    sys.path.insert(0,str(vendor))

from msgdb.c2c.parser import parse_row as parse_c2c
from msgdb.group.exporter import parse_row as parse_group

VERSION = 'existing-msgdb-protobuf-20261103-v1'
FIELD_MAP = {'msg_id':'40001','timestamp':'40050','direction':'40013',
             'sender_uid':'40020','sender_qq':'40033','peer_uid':'40021',
             'peer_qq':'40030','group_id':'40021','group_qq':'40030',
             'msg_type':'40011','subtype':'40012','blob':'40800'}
STRICT_KEYS = {
    'c2c': ('timestamp','sender_uid','sender_qq','peer_uid','peer_qq','msg_type',
            'content_type','proto_ver','inner_ts','text','content'),
    'group': ('timestamp','sender_uid','sender_qq','group_id','group_qq','msg_type',
              'subtype','content_type','text','content'),
}


def text(value):
    if isinstance(value,bytes):
        try:return value.decode('utf-8')
        except UnicodeDecodeError:return None
    return str(value) if value is not None else None


def normalize(table, fields):
    if table not in {'c2c_msg_table','group_msg_table','c2c_messages','group_messages'}:
        return None
    kind='c2c' if table.startswith('c2c') else 'group'
    if table.endswith('_messages'):
        normalized=dict(fields)
    else:
        row={name:fields.get(column) for name,column in FIELD_MAP.items()}
        try:
            normalized=parse_c2c(row).to_db_row() if kind=='c2c' else parse_group(row)
        except Exception as exc:
            normalized={k:v for k,v in row.items() if k!='blob'}
            normalized.update(text=None,content=None,parse_status='unparsed:'+type(exc).__name__)
        card=text(fields.get('40090'));nickname=text(fields.get('40093'))
        normalized['historic_sender_label']=card or nickname
        normalized['historic_sender_label_source']='historic_group_card' if card else ('historic_group_nickname' if nickname else None)
        if fields.get('40800') and (normalized.get('content') is None or normalized.get('parse_status','typed')!='typed'):
            normalized['unparsed_payload_sha256']=hashlib.sha256(bytes(fields['40800'])).hexdigest()
    return kind,normalized


def fingerprint(kind, normalized):
    # Field names/types are fixed; payload/content differences do not get discarded.
    values=[kind]+[normalized.get(key) for key in STRICT_KEYS[kind]]+[normalized.get('unparsed_payload_sha256')]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).digest()

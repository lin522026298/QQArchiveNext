from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'viewer/qq_nt_archive_viewer/src'))
from qq_nt_archive_viewer.archive_store import ArchiveStore
from qq_nt_archive_viewer import summary_cache

spec = importlib.util.spec_from_file_location('daily_fixture_builder', ROOT / 'scripts/Build-QqCanonicalPublication.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def search_store():
    store = ArchiveStore.__new__(ArchiveStore)
    store.message_connection = sqlite3.connect(':memory:')
    store.message_connection.row_factory = sqlite3.Row
    builder.init_message_database(store.message_connection)
    store.identity_connection = None
    store.identity_schema = None
    store._conversation_cache = store._conversation_index = None
    store.cache_dir = None
    store._snapshot_contact_cache = {}
    store._snapshot_group_title_cache = {}
    store._snapshot_member_cache = {}
    return store


class LegacyPaginationTests(unittest.TestCase):
    def test_id_first_pages_keep_global_order_ties_and_minimum_int64_context(self):
        store=search_store();store.account_id=store.storage_account_id='12345'
        try:
            rows=[(-2**63,1)]+[(n,100+n%3) for n in range(1,410)]
            store.message_connection.executemany('INSERT INTO c2c_messages(msg_id,timestamp,direction,sender_uid,sender_qq,peer_qq,peer_uid,msg_type,text,content) VALUES(?,?,0,\'sender\',111,45678,\'peer\',1,\'body\',\'{}\')',rows)
            conversation=store.conversations()[0];latest=store.latest_messages(conversation)
            expected=[row[0] for row in store.message_connection.execute('SELECT msg_id FROM c2c_messages ORDER BY timestamp DESC,msg_id DESC LIMIT 200')][::-1]
            self.assertEqual([row.msg_id for row in latest],expected)
            older=store.older_messages(conversation,latest[0]);self.assertFalse({m.msg_id for m in older}&set(expected))
            newer=store.newer_messages(conversation,older[-1]);self.assertEqual([m.msg_id for m in newer],expected)
            around=store.messages_around(conversation,-2**63)
            self.assertEqual(around[0].msg_id,-2**63)
        finally:store.message_connection.close()


class DailyViewerTests(unittest.TestCase):
    def setUp(self):
        self.store = search_store()

    def tearDown(self):
        self.store.message_connection.close()

    def insert(self, msg_id, timestamp, text, *, group=False):
        table = 'group_messages' if group else 'c2c_messages'
        peer_columns = 'group_id,group_qq,parse_status' if group else 'peer_uid,peer_qq,proto_ver'
        self.store.message_connection.execute(
            f'INSERT INTO {table}(msg_id,timestamp,direction,sender_uid,sender_qq,{peer_columns},msg_type,text,content) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (msg_id,timestamp,0,'sender',123,'peer',456,'ok',1,text,''))

    def test_chinese_substrings_are_complete_even_when_fts_has_a_token_hit(self):
        self.insert(1,1,'研究')
        self.insert(2,2,'今天研究方案')
        self.insert(3,3,'继续研究课题',group=True)
        builder.build_fts(self.store.message_connection)
        self.assertEqual([r.msg_id for r in self.store.search('研究')],[3,2,1])

    def test_literal_wildcards_and_global_pages(self):
        self.insert(1,1,'a%_b')
        self.insert(2,2,'aXXb')
        self.assertEqual([r.msg_id for r in self.store.search('%_')],[1])
        for n in range(3,10):
            self.insert(n,n,'公共关键词',group=n%2==0)
        pages=[self.store.search('关键词',limit=2,offset=n) for n in range(0,8,2)]
        self.assertEqual([r.msg_id for page in pages for r in page],list(range(9,2,-1)))

    def test_cancelled_search_does_not_poison_next_query(self):
        for n in range(2000): self.insert(n,n,'普通文字')
        with self.assertRaises(sqlite3.OperationalError):
            self.store.search('不存在',cancelled=lambda:True)
        self.assertTrue(self.store.search('普通'))

    def test_summary_round_trip_and_changed_source_invalidation(self):
        self.insert(1,1,'测试')
        values=self.store.conversations()
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            summary_cache.save(directory,'a'*64,values)
            restored=summary_cache.load(directory,'a'*64,type(values[0]))
            self.assertEqual(restored,values)
            self.store._set_conversations(restored)
            self.assertEqual(self.store.search('测试')[0].conversation,values[0])
            (directory/('a'*64+'.json')).write_text('broken',encoding='utf-8')
            self.assertIsNone(summary_cache.load(directory,'a'*64,type(values[0])))


if __name__=='__main__': unittest.main()

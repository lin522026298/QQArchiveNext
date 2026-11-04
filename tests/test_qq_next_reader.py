import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'viewer/qq_nt_archive_viewer/src'))
from qq_archive_next.catalog import Catalog
from qq_archive_next.reader import NextArchiveStore
from test_qq_next_catalog import fixture


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.input=self.root/'input.sqlite';fixture(self.input)
        db=sqlite3.connect(self.input)
        db.executemany('INSERT INTO c2c_messages SELECT ?,?,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,?,content FROM c2c_messages WHERE msg_id=1',
          [(n,100+n,('A_研究%计划 TEST' if n%2 else '研究新计划 Test')) for n in range(2,412)])
        db.commit();db.close()
        self.catalog=Catalog(self.root/'catalog.sqlite')
        self.args=dict(source_id='source',account_key='account',binding='fixed',kind='publication')
        self.catalog.import_sqlite(self.input,**self.args)
        self.manifest=self.root/'viewer-account.json'
        self.manifest.write_text(json.dumps({'schema':'qq-next-viewer/1','account_key':'account','catalog':'catalog.sqlite'}),encoding='utf-8')
        self.reader=NextArchiveStore(self.manifest)

    def tearDown(self):self.reader.close();self.catalog.close();self.temp.cleanup()

    def test_replay_search_paging_and_context(self):
        conversation=self.reader.conversations()[0]
        latest=self.reader.latest_messages(conversation)
        self.assertEqual(len(latest),200)
        older=self.reader.older_messages(conversation,latest[0]);self.assertEqual(len(older),200)
        self.assertFalse({m.msg_id for m in latest}&{m.msg_id for m in older})
        self.assertEqual(len(self.reader.search('研究',limit=1000)),411)
        self.assertEqual(len(self.reader.search('研究新',limit=1000)),205)
        self.assertEqual(len(self.reader.search('_研究%',limit=1000)),205)
        self.assertEqual(len(self.reader.search('test',limit=1000)),410)
        hits=self.reader.search('test',limit=201)
        next_hits=self.reader.search('test',limit=201,offset=200)
        self.assertEqual(hits[200].msg_id,next_hits[0].msg_id)
        located=self.reader.messages_around(conversation,hits[0].msg_id)
        self.assertIn(hits[0].msg_id,[m.msg_id for m in located])
        self.assertEqual(self.reader.observations(hits[0].msg_id)[0]['source_id'],'source')
        self.catalog.verify()

    def test_live_summary_reloads_after_old_message_edit_and_backfill(self):
        first,overview,conversations=self.reader.summaries();self.assertEqual(overview['c2c_messages'],411)
        db=sqlite3.connect(self.input)
        db.execute("UPDATE c2c_messages SET text='修订版本' WHERE msg_id=1")
        db.execute('INSERT INTO c2c_messages SELECT -99,1,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,\'回填\',content FROM c2c_messages WHERE msg_id=1')
        db.commit();db.close();self.catalog.import_sqlite(self.input,**self.args)
        second,overview,conversations=self.reader.summaries()
        self.assertGreater(second,first);self.assertEqual(overview['c2c_messages'],412)
        self.assertEqual(conversations[0].message_count,412)
        hit=self.reader.search('修订',limit=10)[0]
        self.assertEqual(len(self.reader.observations(hit.msg_id)),2)

    def test_async_window_load_and_close_during_search(self):
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from qq_nt_archive_viewer.app import MainWindow,create_application
        application=create_application();window=MainWindow(self.manifest,'account',settings_app='QQArchiveNextTests')
        deadline=time.monotonic()+10
        while window._reads and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        self.assertEqual(window.conversations.count(),1)
        window.select_conversation(window.store._conversation_cache[0])
        while window._reads and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        self.assertEqual(len(window.current_messages),200)
        window.global_search.setText('研究');window.search();window.close()
        while window._pool.activeThreadCount() and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        window.close()
        application.processEvents();self.assertEqual(window._pool.activeThreadCount(),0)

    def test_unknown_timestamp_records_remain_reachable_through_every_page(self):
        db=sqlite3.connect(self.input);db.execute('UPDATE c2c_messages SET timestamp=NULL WHERE msg_id IN (1,2,3)');db.commit();db.close()
        self.catalog.import_sqlite(self.input,**self.args)
        conversation=self.reader.conversations()[0];page=self.reader.latest_messages(conversation);all_messages=list(page)
        while page:
            page=self.reader.older_messages(conversation,page[0]);all_messages=page+all_messages
        self.assertEqual(len(all_messages),411);self.assertEqual(len({m.msg_id for m in all_messages}),411)
        unknown=[m for m in all_messages if m.timestamp is None];self.assertEqual(len(unknown),3)
        self.assertTrue(self.reader.newer_messages(conversation,unknown[-1]))
        self.assertIn(unknown[0].msg_id,[m.msg_id for m in self.reader.messages_around(conversation,unknown[0].msg_id)])
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from qq_nt_archive_viewer.app import MainWindow,create_application
        application=create_application();window=MainWindow(self.manifest,'account',settings_app='QQArchiveNextTests')
        deadline=time.monotonic()+5
        while window._reads and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        window.current_conversation=conversation;window.current_messages=unknown;window.current_media={}
        window.render_page('未知时间',media_ready=True)
        self.assertTrue(window.older.isEnabled());self.assertTrue(window.newer.isEnabled())
        window.close();application.processEvents()


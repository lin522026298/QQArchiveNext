from pathlib import Path
import hashlib
import base64
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from qq_archive_next.catalog import Catalog
from qq_archive_next.config import publish,load
from qq_archive_next.history import copy_tree,sha_file,verify
from qq_archive_next.materialize import atomic_json
from qq_archive_next.reader import NextArchiveStore
from qq_archive_next import backup
from qq_archive_next.backup_unified import export,blob_path
from qq_nt_archive_viewer.publication import publication_accounts

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('unified_fixture_builder',ROOT/'scripts/Build-QqCanonicalPublication.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class UnifiedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir()
        self.old=self.root/'original';archive=self.old/'accounts/legacy/archive';archive.mkdir(parents=True)
        structured=archive.parent/'structured';structured.mkdir();message=structured/'nt_msg_export.db'
        db=sqlite3.connect(message);builder.init_message_database(db)
        db.execute("INSERT INTO source_runs VALUES('run-1','official_restore','original-package','heritage/original.db',?,1)",('a'*64,))
        db.execute("INSERT INTO source_runs VALUES('run-2','pc_snapshot','second-package','heritage/second.db',?,2)",('b'*64,))
        db.execute("INSERT INTO c2c_messages(msg_id,timestamp,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,text,content) VALUES(1,100,0,'sender',111,'peer',222,1,1,'共同关键词','{}')")
        db.execute("INSERT INTO c2c_messages SELECT 2,1,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,'旧档专属','{}' FROM c2c_messages WHERE msg_id=1")
        db.execute("INSERT INTO group_messages(msg_id,timestamp,direction,sender_uid,sender_qq,group_id,group_qq,msg_type,subtype,content_type,text,parse_status,content) VALUES(3,50,0,'sender',111,'group',333,1,0,1,'群聊关键词','typed','{}')")
        for n in (1,2,3):
            table='group_messages' if n==3 else 'c2c_messages'
            db.execute('INSERT INTO logical_messages VALUES(?,?,?,?,?,?)',(n,bytes([n])*32,table,'run-1',n,2 if n==3 else 1))
            for run in (('run-1','run-2') if n==3 else ('run-1',)):
                db.execute('INSERT INTO message_provenance VALUES(?,?,?,?,?,?,?)',(n,run,table,n,hashlib.sha256((run+str(n)).encode()).digest(),bytes([n])*32,1))
        db.commit();db.close()
        self.payload=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=');sha=hashlib.sha256(self.payload).hexdigest()
        obj=self.old/'objects/sha256'/sha[:2]/sha;obj.parent.mkdir(parents=True);obj.write_bytes(self.payload)
        heritage=self.old/'heritage';heritage.mkdir();(heritage/'unknown-original.bin').write_bytes(bytes(range(256)))
        index=archive/'index';index.mkdir();db=sqlite3.connect(index/'media_map.sqlite')
        db.executescript('''CREATE TABLE metadata(key TEXT,value TEXT);
          CREATE TABLE assets(asset_id INTEGER PRIMARY KEY,archive_relative TEXT,detected_mime TEXT,cache_class TEXT);
          CREATE TABLE message_media(link_id INTEGER PRIMARY KEY,source_table TEXT,msg_id INTEGER,segment_index INTEGER,media_type TEXT,role TEXT,verification TEXT,note TEXT,filename TEXT,primary_asset_id INTEGER,preview_asset_id INTEGER,playable_asset_id INTEGER,candidate_asset_id INTEGER);''')
        db.execute('INSERT INTO metadata VALUES(?,?)',('schema',json.dumps('qq-media-map/2')))
        db.execute('INSERT INTO assets VALUES(1,?,?,?)',('objects/sha256/'+sha[:2]+'/'+sha,'image/png','original'))
        db.execute("INSERT INTO message_media VALUES(1,'group_msg_table',3,0,'image','primary','verified_content','original hash','photo.png',1,NULL,NULL,NULL)")
        db.execute("INSERT INTO message_media VALUES(2,'c2c_msg_table',2,0,'image','candidate','filename_only','unverified filename','unknown.png',NULL,NULL,NULL,1)")
        db.commit();db.close()
        db=sqlite3.connect(index/'identity.sqlite');builder.init_identity_database(db)
        db.executemany('INSERT INTO metadata VALUES(?,?)',[('schema','qq-identity-index/1'),('account_id','legacy')])
        db.execute("INSERT INTO coverage VALUES('group_message_historic_sender_labels',1,1,1,1,1,0,0)")
        db.execute("INSERT INTO label_values VALUES(1,'旧群名片','historic_group_card')");db.execute('INSERT INTO group_message_labels VALUES(3,1)')
        db.execute("INSERT INTO identity_provenance VALUES(3,'run-1',3,1,1)");db.commit();db.close()
        atomic_json(archive/'archive_manifest.json',{'schema':'qq-archive-viewer/2','account_id':'legacy',
          'publication':{'root':'../../..'},'message_store':{'format':'nt_msg_export/1','path':'../structured/nt_msg_export.db'},
          'media_store':{'mapping_database':'index/media_map.sqlite','object_root':'../../../objects/sha256'},
          'identity_store':{'format':'qq-identity-index/1','path':'index/identity.sqlite'}})
        copied=copy_tree(self.old,self.state/'history/v2',self.state/'history/inventory.jsonl')
        identity=str(uuid.uuid4());atomic_json(self.state/'OWNER.json',{'schema':'qq-next-state/1','id':identity})
        self.config={'schema':'qq-next-config/2','state_id':identity,'auto_capture':False,
          'sources':[{'id':'current','account_key':'account','account_label':'12345','database':str(self.root/'input/missing-nt.db'),'key_file':'private/source.key'}],
          'legacy_sources':[{'id':'legacy','account_key':'account','manifest':'history/v2/accounts/legacy/archive/archive_manifest.json',
            'message_store':'history/v2/accounts/legacy/structured/nt_msg_export.db','messages':3,'observations':4}],
          'history_packages':[{'id':'old','root':'history/v2','inventory':'history/inventory.jsonl',**copied}],
          'application_directory':'application'}
        (self.state/'private').mkdir();(self.state/'private/source.key').write_bytes(b'private-test-key')
        (self.state/'application').mkdir();(self.state/'application/QQArchiveNext.exe').write_bytes(b'fake-test-program')
        atomic_json(self.state/'config.private.json',self.config)
        self.catalog=Catalog(self.state/'catalog.sqlite')
        self.catalog.import_sqlite(message,source_id='legacy',account_key='account',kind='publication',binding=sha_file(message)[1],tables={'c2c_messages','group_messages'})
        self.current=self.root/'current.sqlite';shutil.copyfile(message,self.current)
        self.catalog.import_sqlite(self.current,source_id='current',account_key='account',kind='publication',binding='current',tables={'c2c_messages','group_messages'})
        publish(self.state,self.config)

    def tearDown(self):
        self.catalog.close();self.temp.cleanup()

    def test_single_timeline_search_labels_original_provenance_and_media(self):
        db=sqlite3.connect(self.current);db.execute("UPDATE c2c_messages SET text='当前修订版本' WHERE msg_id=1");db.commit();db.close()
        self.catalog.import_sqlite(self.current,source_id='current',account_key='account',kind='publication',binding='current',tables={'c2c_messages','group_messages'})
        self.old.rename(self.root/'detached-original')
        reader=NextArchiveStore(self.state/'viewer-account.json')
        try:
            self.assertEqual(reader.overview()['c2c_messages'],3)
            self.assertEqual(reader.overview()['media_objects'],1)
            conv=next(c for c in reader.conversations() if c.source_table=='c2c_messages')
            self.assertEqual([m.timestamp for m in reader.latest_messages(conv)],[1,100,100])
            self.assertEqual(len(reader.search('共同关键词')),1);self.assertEqual(len(reader.search('当前修订')),1)
            group=reader.search('群聊关键词')[0];self.assertEqual(group.sender_label,'旧群名片')
            observations=reader.observations(group.msg_id)
            historical=next(o for o in observations if o['source_id']=='legacy')
            self.assertEqual(len(historical['historic_provenance']),2)
            self.assertEqual(historical['historic_identity_provenance'][0]['display_name'],'旧群名片')
            messages=reader.messages_around(group.conversation,group.msg_id)
            link=reader.media_for_messages('group_messages',messages)[group.msg_id][0]
            self.assertEqual(link.primary.path.read_bytes(),self.payload)
            from qq_archive_next.media import external_copy
            editable=external_copy(link.primary.path,'photo.png');editable.write_bytes(b'user-edited')
            self.assertEqual(link.primary.path.read_bytes(),self.payload)
            self.assertTrue(editable.is_relative_to(self.state/'cache'))
            hit=reader.search('旧档专属')[0];old=reader.messages_around(hit.conversation,hit.msg_id)
            candidate=reader.media_for_messages('c2c_messages',old)[hit.msg_id][0]
            self.assertIsNone(candidate.primary);self.assertEqual(candidate.verification,'filename_only')
        finally:reader.close()
        self.assertEqual(verify(self.state,self.config)['accounts'][0]['original_observations'],4)

    def test_complete_point_relocation_regular_copy_and_idle_dedup(self):
        repo=self.root/'repository';first=backup.create(self.state,repo);second=backup.create(self.state,repo)
        self.assertTrue(second['no_op']);self.assertEqual(second['new_object_bytes'],0)
        self.assertEqual(backup.verify_point(repo)['files_reconstructed_and_hashed'],first['files'])
        portable=self.root/'portable';export(repo,portable)
        self.assertTrue((portable/'Restore.cmd').is_file())
        program=portable/'application/QQArchiveNext.exe';program.write_bytes(b'corrupt-test-program')
        with self.assertRaisesRegex(ValueError,'program hash mismatch'):backup.verify_point(portable)
        program.write_bytes(b'fake-test-program')
        detached=self.root/'moved-folder';shutil.copytree(portable,detached)
        self.catalog.close()
        self.state.rename(self.root/'detached-state');self.old.rename(self.root/'detached-original');repo.rename(self.root/'detached-repository')
        destination=self.root/'restored';result=backup.restore(detached,destination)
        self.assertTrue(result['deep_semantic_verification']);self.assertTrue(result['physical_independence'])
        self.assertEqual(result['verification']['history']['accounts'][0]['messages'],3)
        self.assertFalse(load(destination)[1]['auto_capture']);self.assertEqual(publication_accounts(destination/'viewer_manifest.json')[0].display_id,'12345')
        reader=NextArchiveStore(destination/'viewer-account.json')
        try:self.assertEqual(len(reader.search('关键词')),2)
        finally:reader.close()

    def test_reuse_immutable_cli_preserves_complete_checks_and_mutable_files(self):
        from qq_archive_next.__main__ import main
        repo=self.root/'repository';first=backup.create(self.state,repo)
        _,point=backup.read_point(repo);entries=dict(backup.point_files(repo,point))
        destination=self.root/'linked-restore';report=self.root/'linked-report.json'
        code=main(['--report',str(report),'restore','--repository',str(repo),'--destination',str(destination),'--reuse-immutable'])
        self.assertEqual(code,0);receipt=json.loads(report.read_text(encoding='utf-8'))
        self.assertTrue(receipt['immutable_reuse_requested']);self.assertFalse(receipt['physical_independence'])
        self.assertGreater(receipt['hardlinked_bytes'],0);self.assertTrue(receipt['deep_semantic_verification'])
        self.assertEqual(receipt['file_hashes_verified'],first['files'])
        original=next(n for n in entries if n.endswith('unknown-original.bin'))
        self.assertTrue(os.path.samefile(destination/original,blob_path(repo,entries[original]['blob'])))
        self.assertFalse(os.path.samefile(destination/'catalog.sqlite',self.state/'catalog.sqlite'))
        self.assertFalse(os.path.samefile(destination/'config.private.json',self.state/'config.private.json'))
        self.assertFalse(load(destination)[1]['auto_capture'])

    def test_reuse_immutable_falls_back_to_complete_regular_copy(self):
        repo=self.root/'repository';first=backup.create(self.state,repo)
        with patch('qq_archive_next.backup.os.link',side_effect=OSError('links unavailable')):
            receipt=backup.restore(repo,self.root/'fallback-restore',hardlink_immutable=True)
        self.assertTrue(receipt['immutable_reuse_requested']);self.assertTrue(receipt['physical_independence'])
        self.assertEqual(receipt['hardlinked_bytes'],0);self.assertEqual(receipt['file_hashes_verified'],first['files'])
        self.assertTrue(receipt['deep_semantic_verification']);self.assertEqual(receipt['verification']['logical_messages'],3)

    def test_missing_corrupt_original_and_partial_restore_are_refused(self):
        repo=self.root/'repository';backup.create(self.state,repo)
        _,point=backup.read_point(repo);entries=dict(backup.point_files(repo,point))
        name=next(n for n in entries if n.endswith('unknown-original.bin'));path=blob_path(repo,entries[name]['blob'])
        path.write_bytes(b'x'*path.stat().st_size)
        with self.assertRaisesRegex(ValueError,'hash mismatch'):backup.restore(repo,self.root/'failed')
        self.assertTrue((self.root/'failed/RESTORING.json').exists());self.assertFalse((self.root/'failed/RESTORE_RECEIPT.json').exists())
        with self.assertRaises(ValueError):load(self.root/'failed')
        path.unlink()
        with self.assertRaises(FileNotFoundError):backup.verify_point(repo)

    def test_building_marker_and_bad_account_binding_cannot_open(self):
        atomic_json(self.state/'BUILDING.json',{'state':'test'})
        with self.assertRaises(ValueError):load(self.state)
        with self.assertRaises(Exception):NextArchiveStore(self.state/'viewer-account.json')
        (self.state/'BUILDING.json').unlink()
        self.config['legacy_sources'][0]['account_key']='other'
        atomic_json(self.state/'config.private.json',self.config)
        with self.assertRaises(ValueError):load(self.state)

    def test_current_native_message_keeps_verified_media_in_unified_reader(self):
        from msgdb.proto.c2c_40800_pb2 import MsgBody
        from qq_archive_next.media import capture_media
        body=MsgBody();part=body.content.add();part.content_type=2;part.md5_raw=hashlib.md5(self.payload).digest();part.filename='native.png'
        plain=self.root/'native.sqlite';db=sqlite3.connect(plain)
        db.execute('CREATE TABLE c2c_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,"40013" INTEGER,"40020" TEXT,"40033" INTEGER,"40021" TEXT,"40030" INTEGER,"40011" INTEGER,"40012" INTEGER,"40800" BLOB)')
        db.execute('INSERT INTO c2c_msg_table VALUES(2001,200,0,\'sender\',111,\'peer\',222,2,0,?)',(body.SerializeToString(),));db.commit();db.close()
        self.catalog.import_sqlite(plain,source_id='native-live',account_key='account',kind='native',binding='native-test')
        media=self.root/'media/Pic';media.mkdir(parents=True);(media/'native.png').write_bytes(self.payload)
        capture_media(self.state,{'id':'native-live','account_key':'account','media_root':str(media.parent)})
        reader=NextArchiveStore(self.state/'viewer-account.json')
        try:
            conv=next(c for c in reader.conversations() if c.source_table=='c2c_messages')
            messages=reader.latest_messages(conv);native=next(m for m in messages if m.timestamp==200)
            link=reader.media_for_messages('c2c_messages',messages)[native.msg_id][0]
            self.assertEqual(link.verification,'verified_content');self.assertEqual(link.primary.path.read_bytes(),self.payload)
            self.assertEqual(reader.overview()['media_objects'],1)
        finally:reader.close()

    def test_message_page_alias_work_is_bounded_by_page_size(self):
        plain=self.root/'many-messages.sqlite';db=sqlite3.connect(plain)
        db.execute('CREATE TABLE c2c_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,"40013" INTEGER,"40020" TEXT,"40033" INTEGER,"40021" TEXT,"40030" INTEGER,"40011" INTEGER,"40012" INTEGER,"40800" BLOB)')
        db.executemany('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?,?,?)',
          ((n,n+1000,0,'sender',111,'peer',222,1,0,b'') for n in range(10000,15000)))
        db.commit();db.close()
        self.catalog.import_sqlite(plain,source_id='many',account_key='account',kind='native',binding='many-test')
        ids=[r[0] for r in self.catalog.db.execute("SELECT published_id FROM message_head WHERE source_id='many' LIMIT 200")]
        reader=NextArchiveStore(self.state/'viewer-account.json');ticks=[0]
        def bounded():
            ticks[0]+=1
            return int(ticks[0]>200)
        reader.message_connection.set_progress_handler(bounded,100)
        try:
            aliases=reader._aliases(ids,'c2c_messages')
            self.assertEqual({r['published_id'] for r in aliases},set(ids))
            self.assertTrue(all(r['source_id']=='many' for r in aliases))
        finally:
            reader.message_connection.set_progress_handler(None,0);reader.close()

    def test_equal_counts_cannot_hide_a_missing_original_message_identity(self):
        self.catalog.db.execute("UPDATE message_head SET row_key=? WHERE source_id='legacy' AND source_table='group_messages'",(b'x'*32,));self.catalog.db.commit()
        with self.assertRaisesRegex(ValueError,'no exact unified identity'):verify(self.state,self.config)

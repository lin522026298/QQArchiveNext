from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
import io
import math
import struct
import wave
from threading import Event

from qq_next_fixture import native,wrap,PASSWORD
from qq_archive_next.config import load,publish
from qq_archive_next.guard import WriterLock
from qq_archive_next.materialize import atomic_json
from qq_archive_next.service import Coordinator,request_stop,request_action
from qq_archive_next import backup,media


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=self.root/'state';self.state.mkdir();self.source=self.root/'input'
        self.source.mkdir();native(self.source/'plain.sqlite');wrap(self.source/'plain.sqlite',self.source/'nt_msg.db')
        self.media=self.source/'nt_data';(self.media/'Pic').mkdir(parents=True)
        self.owner=str(uuid.uuid4())
        atomic_json(self.state/'OWNER.json',{'schema':'qq-next-state/1','id':self.owner})
        self.config={'schema':'qq-next-config/1','state_id':self.owner,'auto_capture':False,'sources':[
          {'id':'current-test','account_key':'account-test','account_label':'12345','database':str(self.source/'nt_msg.db'),
           'key_file':'private/source.key','media_root':str(self.media)}]}
        atomic_json(self.state/'config.private.json',self.config)
        (self.state/'private').mkdir();(self.state/'private/source.key').write_bytes(PASSWORD)

    def tearDown(self):self.temp.cleanup()

    def capture(self,**kwargs):return Coordinator(self.state).run(once=True,**kwargs)

    def test_writer_lock_contention_and_owner_validation(self):
        with WriterLock(self.state):
            with self.assertRaisesRegex(RuntimeError,'already running'):
                with WriterLock(self.state):pass
        changed=dict(self.config);changed['state_id']=str(uuid.uuid4())
        atomic_json(self.state/'config.private.json',changed)
        with self.assertRaisesRegex(ValueError,'owner mismatch'):load(self.state)

    def test_capture_noop_and_recoverable_failed_key(self):
        status=self.capture(media=False)
        self.assertEqual(status['sources']['current-test']['state'],'ok')
        self.assertFalse((self.state/'views/current-test/current.pending').exists())
        con=sqlite3.connect(self.state/'catalog.sqlite');before=con.execute('SELECT count(*) FROM capture').fetchone()[0];con.close()
        status=self.capture(media=False);self.assertTrue(status['sources']['current-test']['import']['no_op'])
        con=sqlite3.connect(self.state/'catalog.sqlite');self.assertEqual(con.execute('SELECT count(*) FROM capture').fetchone()[0],before);con.close()
        (self.state/'private/source.key').write_bytes(b'wrong')
        status=self.capture(media=False)
        self.assertEqual(status['sources']['current-test']['state'],'retry')
        self.assertIn('last_success',status['sources']['current-test'])

    def test_stop_request_is_bound_to_current_runtime(self):
        runner=Coordinator(self.state);runner.save_status();request_stop(self.state)
        self.assertTrue(runner.cancelled())
        other=Coordinator(self.state)
        self.assertFalse(other.cancelled())

    def test_prepared_migration_allows_maintenance_but_cannot_fork_capture(self):
        self.capture(media=False);origin=str(uuid.uuid4())
        db=sqlite3.connect(self.state/'catalog.sqlite');db.execute('CREATE TABLE unified_origin(state_id TEXT PRIMARY KEY,closed INTEGER)')
        db.execute('INSERT INTO unified_origin VALUES(?,0)',(origin,));db.commit();db.close()
        self.config['integration_origin_state']=origin;atomic_json(self.state/'config.private.json',self.config)
        with self.assertRaisesRegex(ValueError,'尚未切换'):self.capture(media=False)
        runner=Coordinator(self.state);runner.commands.put(('verify',{}))
        self.assertEqual(runner.run(once=True,capture_sources=False)['maintenance']['state'],'ok')
        db=sqlite3.connect(self.state/'catalog.sqlite');db.execute('UPDATE unified_origin SET closed=1');db.commit();db.close()
        self.assertEqual(self.capture(media=False)['sources']['current-test']['state'],'ok')

    def test_remote_backup_request_is_queued_once_for_its_runtime(self):
        runner=Coordinator(self.state);runner.save_status();request_action(self.state,'backup')
        self.assertFalse(runner.cancelled());self.assertEqual(runner.commands.qsize(),1)
        runner.last_control=0;self.assertFalse(runner.cancelled());self.assertEqual(runner.commands.qsize(),1)
        other=Coordinator(self.state);self.assertFalse(other.cancelled());self.assertTrue(other.commands.empty())

    def test_paused_maintenance_never_attaches_to_live_source(self):
        self.capture(media=False)
        (self.state/'private/source.key').unlink()
        self.config['client_exe']='missing-qq.exe';self.config['client_version']='untested'
        atomic_json(self.state/'config.private.json',self.config)
        runner=Coordinator(self.state);runner.commands.put(('verify',{}))
        status=runner.run(once=True,capture_sources=False,media=False)
        self.assertEqual(status['maintenance']['state'],'ok')
        self.assertTrue(status['maintenance']['result']['fts_integrity_checked'])
        self.assertEqual(status['maintenance']['result']['capture'],1)

    def test_publication_contract(self):
        self.capture(media=False)
        from qq_nt_archive_viewer.publication import publication_accounts
        result=publication_accounts(self.state/'viewer_manifest.json')
        self.assertEqual(result[0].display_id,'12345')
        self.assertEqual(publication_accounts(self.state/'viewer-account-test.json')[0].display_id,'12345')

    def test_media_dedup_noop_and_exact_message_link(self):
        payload=b'\x89PNG\r\n\x1a\n'+bytes(range(256))*4
        (self.media/'Pic/a.png').write_bytes(payload);(self.media/'Pic/b.png').write_bytes(payload)
        report=media.capture_media(self.state,self.config['sources'][0])
        self.assertEqual(report['bytes_copied'],len(payload));self.assertEqual(report['observations_added'],2)
        report=media.capture_media(self.state,self.config['sources'][0]);self.assertEqual(report['observations_added'],0);self.assertEqual(report['bytes_copied'],0)
        from msgdb.proto.c2c_40800_pb2 import MsgBody
        body=MsgBody();segment=body.content.add();segment.content_type=2;segment.md5_raw=hashlib.md5(payload).digest();segment.filename='a.png'
        fields={'40011':2,'40800':body.SerializeToString()}
        link=media.links(self.state,'account-test',[(1,'c2c_messages',fields)])[1][0]
        self.assertEqual(link.verification,'verified_content');self.assertEqual(link.primary.path.read_bytes(),payload)
        self.assertFalse((self.state/'cache/open').exists())
        original=link.primary.path
        opened=media.external_copy(original,'saved.png');opened.write_bytes(b'user edits')
        self.assertEqual(original.read_bytes(),payload)
        another=media.external_copy(original,'saved.png')
        self.assertNotEqual(opened,another);self.assertEqual(opened.read_bytes(),b'user edits')
        segment.md5_raw=b'0'*16
        fields['40800']=body.SerializeToString()
        link=media.links(self.state,'account-test',[(1,'c2c_messages',fields)])[1][0]
        self.assertIsNone(link.primary)
        self.assertEqual(media.verify(self.state)['objects_verified'],1)

    def test_backup_dedup_and_independent_restore(self):
        (self.media/'Pic/a.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'example')
        self.capture()
        repo=self.root/'backups';first=backup.create(self.state,repo);second=backup.create(self.state,repo)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['no_op']);self.assertEqual(second['new_object_bytes'],0)
        (self.media/'Pic/b.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'another example')
        media.capture_media(self.state,self.config['sources'][0])
        third=backup.create(self.state,repo)
        self.assertNotEqual(third['id'],second['id']);self.assertEqual(third['files'],second['files']+1)
        target=self.root/'restored';receipt=backup.restore(repo,target)
        self.assertTrue(receipt['deep_semantic_verification']);self.assertEqual(receipt['verification']['raw_versions_verified'],1)
        self.assertFalse((target/'private/source.key').exists());self.assertFalse(load(target)[1]['auto_capture'])
        self.assertEqual(receipt['verification']['media']['objects_verified'],2)
        with self.assertRaisesRegex(ValueError,'new destination'):backup.restore(repo,target)

    def test_corrupt_backup_never_gets_success_receipt(self):
        self.capture(media=False);repo=self.root/'backups';backup.create(self.state,repo)
        _,point=backup.read_point(repo)
        db=sqlite3.connect(repo/'blocks.sqlite');db.execute('DROP TRIGGER block_no_update')
        identity,data=db.execute('SELECT id,data FROM block WHERE kind=0 AND byte_count>100 LIMIT 1').fetchone()
        data=bytearray(data);data[-1]^=1;db.execute('UPDATE block SET data=? WHERE id=?',(bytes(data),identity));db.commit();db.close()
        target=self.root/'broken'
        with self.assertRaises(Exception):backup.restore(repo,target)
        self.assertTrue((target/'RESTORING.json').exists());self.assertFalse((target/'RESTORE_RECEIPT.json').exists())

    def test_new_corrupt_cas_cannot_publish_a_backup_point(self):
        (self.media/'Pic/a.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'example')
        self.capture()
        object_path=next(p for p in (self.state/'objects').rglob('*') if p.is_file())
        data=bytearray(object_path.read_bytes());data[-1]^=1;object_path.write_bytes(data)
        repo=self.root/'backups'
        with self.assertRaisesRegex(ValueError,'CAS identity'):backup.create(self.state,repo)
        self.assertFalse((repo/'refs.json').exists())

    def test_old_backup_point_survives_block_pack_upgrade(self):
        self.capture(media=False);repo=self.root/'backups'
        old=backup.create_legacy(self.state,repo)
        new=backup.create(self.state,repo)
        self.assertGreater(new['legacy_blocks_seeded'],0)
        self.assertEqual(backup.verify_point(repo,old['id'])['files_reconstructed_and_hashed'],old['files'])
        self.assertEqual(backup.verify_point(repo,new['id'])['files_reconstructed_and_hashed'],new['files'])
        backup.restore(repo,self.root/'old-restored',identity=old['id'])
        backup.restore(repo,self.root/'new-restored',identity=new['id'])

    def test_streamed_silk_derivative_is_rebuildable(self):
        import pysilk
        pcm=b''.join(struct.pack('<h',int(1000*math.sin(2*math.pi*440*n/24000))) for n in range(4800))
        encoded=io.BytesIO();pysilk.encode(io.BytesIO(pcm),encoded,24000,24000)
        source=self.root/'synthetic.silk';source.write_bytes(encoded.getvalue())
        (self.media/'Ptt').mkdir();(self.media/'Ptt/synthetic.silk').write_bytes(encoded.getvalue())
        media.capture_media(self.state,self.config['sources'][0])
        from msgdb.proto.c2c_40800_pb2 import MsgBody
        body=MsgBody();part=body.content.add();part.content_type=4;part.md5_raw=hashlib.md5(encoded.getvalue()).digest()
        link=media.links(self.state,'account-test',[(1,'c2c_messages',{'40011':6,'40800':body.SerializeToString()})])[1][0]
        self.assertEqual(link.playable.detected_mime,'audio/x-qq-silk')
        self.assertFalse((self.state/'cache/audio').exists())
        wav=media.voice_wav(self.state,source)
        with wave.open(str(wav),'rb') as output:
            self.assertEqual(output.getframerate(),24000);self.assertEqual(output.getnchannels(),1);self.assertGreater(output.getnframes(),0)
        self.assertEqual(media.voice_wav(self.state,source),wav)

    def test_unified_window_status_and_safe_exit(self):
        import os,time
        os.environ['QT_QPA_PLATFORM']='offscreen'
        self.capture(media=False)
        from qq_nt_archive_viewer.app import create_application
        from qq_archive_next.control_app import ControlWindow
        application=create_application();window=ControlWindow(self.state)
        deadline=time.monotonic()+5
        while window._reads and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        self.assertEqual(window.source_select.count(),1)
        window.exit_archive()
        while window._pool.activeThreadCount() and time.monotonic()<deadline:application.processEvents();time.sleep(.002)
        window.close();application.processEvents()

    def test_automatic_backup_skips_unchanged_archive_revision(self):
        self.capture(media=False)
        from qq_archive_next.catalog import Catalog
        runner=Coordinator(self.state);runner.config['auto_backup']=True
        catalog=Catalog(self.state/'catalog.sqlite')
        try:
            runner.automatic_backup(catalog);self.assertEqual(runner.commands.qsize(),1)
            self.assertTrue(runner.maintenance(catalog))
            self.assertEqual(runner.status['maintenance']['state'],'ok')
            runner.last_backup_check=0;runner.automatic_backup(catalog)
            self.assertTrue(runner.commands.empty())
        finally:catalog.close()


"""Cross-interface acceptance, not a second implementation of the readers."""
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_qq_unified_archive as archive_fixture
import test_qq_next_operations as capture_fixture
from qq_archive_next.product import Product,Jobs,capabilities,runtime
from qq_archive_next.web import Host
from qq_archive_next.service import Coordinator
from qq_archive_next.config import load,inside,_inside_resolved
from qq_archive_next.materialize import atomic_json
from qq_archive_next import media


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.fixture=archive_fixture.UnifiedArchiveTests('runTest');self.fixture.setUp()
        self.root=self.fixture.state;self.product=Product(self.root)
    def tearDown(self):self.fixture.tearDown()

    def test_unified_browse_search_timeline_exact_sources_and_media(self):
        product=self.product;account={'account':'account'}
        self.assertIn('restore',capabilities()['operations'])
        self.assertEqual(product.execute('accounts')[0]['c2c_messages'],2)
        conversations=product.execute('conversations',account)
        self.assertEqual(sum(c['message_count'] for c in conversations),3)
        timeline=product.execute('timeline',account)
        self.assertEqual([m['timestamp'] for m in timeline],[1,50,100])
        before=product.execute('timeline',account|{'before':{'timestamp':100,'msg_id':timeline[-1]['msg_id']}})
        self.assertEqual([m['timestamp'] for m in before],[1,50])
        after=product.execute('timeline',account|{'at':49})
        self.assertEqual([m['timestamp'] for m in after],[50,100])
        found=product.execute('search',account|{'query':'群聊关键词'})[0]
        page=product.execute('messages',account|{'conversation':found['conversation']['key'],'around':found['msg_id']})
        self.assertEqual(page[0]['sender_label'],'旧群名片')
        source=product.execute('observations',account|{'msg_id':found['msg_id']})
        self.assertEqual(len(source),2)
        self.assertEqual(len(next(v for v in source if v['source_id']=='legacy')['historic_provenance']),2)
        links=product.execute('media',account|{'msg_id':found['msg_id'],'source_table':'group_messages'})
        self.assertEqual(links[0]['verification'],'verified_content')
        self.assertEqual(Path(links[0]['primary']['path']).read_bytes(),self.fixture.payload)
        with self.assertRaises(ValueError):product.execute('messages',{'account':'other','conversation':conversations[0]['key']})

    def test_complete_backup_pin_verify_restore_export_and_fresh_target_guard(self):
        repo=self.fixture.root/'repository';args={'repository':str(repo)}
        point=self.product.execute('backup',args|{'pin':True})
        listing=self.product.execute('backups',args)['points']
        self.assertTrue(listing[0]['pinned']);self.assertTrue(listing[0]['complete_originals'])
        self.assertTrue(self.product.execute('backup.verify',args)['files_reconstructed_and_hashed']>0)
        self.assertFalse(self.product.execute('backup.pin',args|{'pinned':False})['pinned'])
        target=self.fixture.root/'restored'
        result=self.product.execute('restore',args|{'destination':str(target)})
        self.assertTrue(result['deep_semantic_verification'])
        self.assertEqual([m['text'] for m in Product(target).execute('timeline',{'account':'account'})],[m['text'] for m in self.product.execute('timeline',{'account':'account'})])
        self.assertFalse(load(target)[1]['auto_capture'])
        with self.assertRaisesRegex(ValueError,'new destination'):self.product.execute('restore',args|{'destination':str(target)})
        with self.assertRaisesRegex(ValueError,'overlaps'):self.product.execute('restore',args|{'destination':str(self.root/'bad')})
        bundle=self.fixture.root/'portable'
        self.product.execute('export-portable',args|{'destination':str(bundle)})
        self.assertTrue((bundle/'Open.cmd').exists())
        self.assertEqual(self.product.execute('backup.verify',{'repository':str(bundle)})['point'],point['id'])
        with self.assertRaisesRegex(ValueError,'overlap'):
            from qq_archive_next.backup import restore
            restore(bundle,bundle/'arbitrary-child')
        inside_bundle=self.product.execute('restore',{'repository':str(bundle),'destination':str(bundle/'Recovered')})
        self.assertTrue(inside_bundle['deep_semantic_verification'])
        self.assertEqual(Product(bundle/'Recovered').execute('accounts')[0]['c2c_messages'],2)

    def test_settings_validation_and_default_media_interval(self):
        self.assertEqual(self.product.execute('settings')['media_seconds'],1800)
        result=self.product.execute('settings',{'settings':{'media_seconds':1800,'auto_backup':True}})
        self.assertTrue(result['auto_backup'])
        before=(self.root/'config.private.json').read_bytes()
        for value in ({'media_seconds':0},{'media_seconds':True},{'sources':[]}):
            with self.assertRaises(ValueError):self.product.execute('settings',{'settings':value})
        self.assertEqual((self.root/'config.private.json').read_bytes(),before)

    def test_repository_maintenance_uses_the_shared_writer_and_reader(self):
        repo=self.fixture.root/'repository';args={'repository':str(repo)}
        point=self.product.execute('backup',args|{'pin':True})
        self.assertIn('repository.compact',capabilities()['operations'])
        self.assertEqual(self.product.execute('repository.status',args)['keep'],[point['id']])
        self.assertEqual(self.product.execute('repository.compact',args)['state'],'committed')
        self.assertEqual(self.product.execute('repository.recover',args)['state'],'clean')
        self.assertEqual(self.product.execute('backup.verify',args)['point'],point['id'])

    def test_unchanged_archive_still_schedules_due_repository_compaction(self):
        coordinator=Coordinator(self.root);coordinator.config['auto_backup']=True
        atomic_json(self.root/'backup_receipt.private.json',{'revision':coordinator.archive_revision(self.fixture.catalog)})
        with patch('qq_archive_next.repository_gc.due',return_value=True):coordinator.automatic_backup(self.fixture.catalog)
        self.assertEqual(coordinator.commands.get_nowait()[0],'compact')

    def test_program_only_upgrade_schedules_complete_backup(self):
        manifest=self.root/'application/release_verification.json';manifest.write_text('old bound release',encoding='utf-8')
        coordinator=Coordinator(self.root);coordinator.config['auto_backup']=True
        atomic_json(self.root/'backup_receipt.private.json',{'revision':coordinator.archive_revision(self.fixture.catalog)})
        coordinator.automatic_backup(self.fixture.catalog)
        self.assertTrue(coordinator.commands.empty())
        manifest.write_text('new bound release',encoding='utf-8');coordinator.last_backup_check=0
        coordinator.automatic_backup(self.fixture.catalog)
        self.assertEqual(coordinator.commands.get_nowait()[0],'backup')

    def test_async_cancel_and_recover_interrupted_jobs(self):
        jobs=Jobs(self.product)
        def work(operation,args,*,cancelled):
            while not cancelled():time.sleep(.01)
            raise InterruptedError('checkpoint reached')
        try:
            with patch.object(self.product,'execute',side_effect=work):
                task=jobs.submit('verify',{})
                for _ in range(100):
                    if jobs.get(task['id'])['state']=='running':break
                    time.sleep(.01)
                jobs.cancel(task['id'])
                for _ in range(100):
                    if jobs.get(task['id'])['state']=='cancelled':break
                    time.sleep(.01)
                self.assertEqual(jobs.get(task['id'])['state'],'cancelled')
        finally:jobs.close()
        path=jobs.root/('a'*32+'.json');atomic_json(path,{'id':'a'*32,'operation':'restore','state':'running'})
        restarted=Jobs(self.product)
        try:self.assertEqual(restarted.get('a'*32)['state'],'interrupted')
        finally:restarted.close()

    def test_local_http_security_media_range_and_separate_listener_lifecycle(self):
        host=Host(self.root,autostart=False)
        def send(server,path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            data=None if body is None else json.dumps(body).encode()
            supplied={'X-QQ-Token':server.token,'Origin':server.url}
            if body is not None:supplied['Content-Type']='application/json'
            supplied.update(headers or {})
            connection.request('GET' if body is None else 'POST',path,body=data,headers=supplied)
            response=connection.getresponse();result=(response.status,dict(response.getheaders()),response.read());connection.close();return result
        try:
            code,headers,html=send(host.native,'/')
            self.assertEqual(code,200);self.assertIn(b'QQ_BOOTSTRAP',html);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
            self.assertEqual(send(host.native,'/api/status',headers={'X-QQ-Token':'bad'})[0],403)
            self.assertEqual(send(host.native,'/api/status',headers={'Host':'attacker.invalid'})[0],403)
            self.assertEqual(send(host.native,'/api/call',{'operation':'status'},headers={'Origin':'https://attacker.invalid'})[0],403)
            self.assertEqual(send(host.native,'/../config.private.json')[0],400)
            task=json.loads(send(host.native,'/api/call',{'operation':'timeline','arguments':{'account':'account'}})[2])
            for _ in range(100):
                result=json.loads(send(host.native,'/api/job?id='+task['id'])[2])
                if result['state']=='completed':break
                time.sleep(.02)
            self.assertEqual(result['state'],'completed')
            image=next(m for m in result['result'] if m['source_table']=='group_messages')['media'][0]['primary']
            code,headers,payload=send(host.native,image['url'],headers={'Range':'bytes=1-10'})
            self.assertEqual(code,206);self.assertEqual(payload,self.fixture.payload[1:11]);self.assertEqual(headers['Content-Range'],f'bytes 1-10/{len(self.fixture.payload)}')
            self.assertEqual(send(host.native,image['url']+'x')[0],403)
            web=host.set_web(True);browser=host.browser
            self.assertNotEqual(browser.token,host.native.token)
            self.assertEqual(send(browser,'/api/status',headers={'X-QQ-Token':host.native.token})[0],403)
            self.assertEqual(send(browser,'/api/web',{'enabled':False})[0],404)
            self.assertEqual(send(browser,'/api/notifications/next',{})[0],404)
            important={'issues':[{'id':'test:integrity','important':True,'severity':'critical','title':'校验失败','summary':'验收夹具','target':'health/test'}]}
            with patch.object(host,'status',return_value={'health':important,'desktop':{'important_notifications':True}}), tempfile.TemporaryDirectory() as notices, patch.dict(os.environ,{'QQ_ARCHIVE_NOTIFICATION_HOME':notices}):
                self.assertIsNotNone(json.loads(send(host.native,'/api/notifications/next',{})[2])['notice'])
                self.assertIsNone(json.loads(send(host.native,'/api/notifications/next',{})[2])['notice'])
            host.set_web(False)
            self.assertEqual(send(host.native,'/api/status')[0],200)
            self.assertEqual(browser.token,'')
            self.assertEqual(self.product.execute('jobs')[0]['state'],'completed')
            self.assertTrue(self.product.execute('web',{'enabled':True})['enabled'])
            self.assertFalse(self.product.execute('web',{'enabled':False})['enabled'])
        finally:host.close()


class WriterInboxTests(unittest.TestCase):
    def setUp(self):
        self.fixture=capture_fixture.OperationsTests('runTest');self.fixture.setUp();self.fixture.capture()
    def tearDown(self):self.fixture.tearDown()
    def test_writer_requests_cancel_without_stopping_capture_and_keep_source_checks(self):
        root=self.fixture.state;coordinator=Coordinator(root)
        thread=threading.Thread(target=lambda:coordinator.run(capture_sources=False),daemon=True);thread.start()
        try:
            for _ in range(100):
                if runtime(root):break
                time.sleep(.02)
            self.assertTrue(runtime(root))
            product=Product(root)
            result=product.execute('settings',{'settings':{'media_seconds':1800}})
            self.assertEqual(result['media_seconds'],1800)
            point=product.execute('backup',{'repository':str(self.fixture.root/'backup')})
            self.assertTrue(point['id'])
            self.assertFalse(product.execute('backup.pin',{'repository':str(self.fixture.root/'backup'),'pinned':False})['pinned'])
            original=media.verify
            def slow(*args,**kwargs):
                while not kwargs['cancelled']():time.sleep(.01)
                raise InterruptedError('verify checkpoint')
            event=threading.Event();outcome=[]
            def verify():
                try:product.execute('verify',cancelled=event.is_set)
                except InterruptedError:outcome.append('cancelled')
            with patch.object(media,'verify',side_effect=slow):
                worker=threading.Thread(target=verify);worker.start();time.sleep(.8);event.set();worker.join(12)
                self.assertFalse(worker.is_alive());self.assertEqual(outcome,['cancelled']);self.assertTrue(runtime(root))
        finally:coordinator.stop.set();thread.join(12)
        self.assertFalse(thread.is_alive())

    def test_path_reuse_keeps_escape_symlink_and_replaced_source_checks(self):
        root=self.fixture.state;source=self.fixture.config['sources'][0]
        pic=self.fixture.media/'Pic/a.png';pic.write_bytes(b'first')
        media.capture_media(root,source)
        pic.write_bytes(b'second, changed')
        result=media.capture_media(root,source)
        self.assertEqual(result['observations_added'],1)
        for relative in ('../escape','C:/escape','a/../../escape','a\\b'):
            with self.assertRaises(ValueError):_inside_resolved(root.resolve(),relative)
        outside=self.fixture.root/'outside';outside.mkdir();(outside/'secret').write_bytes(b'outside')
        link=self.fixture.media/'Pic/link'
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError:
            if os.name!='nt':raise
            result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
            if result.returncode:self.skipTest('reparse-point creation unavailable')
        with self.assertRaises(ValueError):inside(self.fixture.media,'Pic/link/secret')
        result=media.capture_media(root,source,full=True)
        self.assertEqual(result['files_seen'],1)
        db=media.connect(root)
        try:
            with db:
                db.execute('DROP TRIGGER asset_no_update') # Simulate a damaged, untrusted index in this isolated fixture.
                db.execute("UPDATE asset SET relative='../outside/secret' WHERE sha256=(SELECT sha256 FROM observation ORDER BY id DESC LIMIT 1)")
        finally:db.close()
        with self.assertRaises(ValueError):media.capture_media(root,source)


if __name__=='__main__':unittest.main()

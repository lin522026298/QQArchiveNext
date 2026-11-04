import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_qq_next_operations as fixtures
import test_qq_unified_archive as archives
from qq_archive_next import health,desktop
from qq_archive_next.cipher import AuthenticationError
from qq_archive_next.config import load
from qq_archive_next.materialize import atomic_json,update
from qq_archive_next.product import Product,Jobs,job_summaries
from qq_archive_next.service import Coordinator


class DailyProductTests(unittest.TestCase):
    def setUp(self):
        self.fixture=archives.UnifiedArchiveTests('runTest');self.fixture.setUp()
        self.root=self.fixture.state
        self.env=patch.dict(os.environ,{'QQ_ARCHIVE_NOTIFICATION_HOME':str(self.fixture.root/'notices')});self.env.start()
    def tearDown(self):self.env.stop();self.fixture.tearDown()

    def state(self,failure=None):
        source={'state':'retry' if failure else 'ok'}
        if failure:source['capture_issue']=failure
        return {'state':'idle','sources':{'current':source}}

    def test_transient_retries_quiet_and_important_incidents_persistently_throttled(self):
        now=1_000_000
        failure=None;delays=[]
        for _ in range(8):
            failure=health.retry_failure(AuthenticationError('read-only input freeze exceeded its checkpoint guard budget'),failure,now=now)
            delays.append(failure['retry_seconds'])
        self.assertEqual(delays,[60,120,240,480,900,900,900,900])
        state=self.state(failure);config={'auto_capture':True}
        info=health.report(self.root,config,state,state,[],now=now+100)
        self.assertEqual(info['important_count'],0);self.assertIsNone(health.next_notice(self.root,info,now=now+100))
        stalled=health.report(self.root,config,state,state,[],now=now+1801)
        self.assertEqual(stalled['important_count'],1)
        first=health.next_notice(self.root,stalled,now=now+1801);self.assertTrue(first['target'].startswith('health/'))
        self.assertIsNone(health.next_notice(self.root,stalled,now=now+1802))
        # A recovery and a repeated same fault must not reset the notice cooldown.
        self.assertEqual(health.report(self.root,config,self.state(),self.state(),[],now=now+1900)['important_count'],0)
        self.assertIsNone(health.next_notice(self.root,stalled,now=now+3000))
        reloaded=json.loads(health.notice_path(self.root).read_text())
        self.assertIn(first['id'],reloaded['sent'])
        self.assertIsNotNone(health.next_notice(self.root,stalled,now=now+1801+21600))

    def test_true_faults_specific_actions_and_historic_failed_start_resolves(self):
        now=1_000_000;failure=health.retry_failure(ValueError('QQ client version changed; capture stopped pending a tested new binding'),now=now)
        state=self.state(failure)
        issue=health.report(self.root,{'auto_capture':True},state,state,[],now=now)['issues'][0]
        self.assertEqual(issue['code'],'client_version');self.assertTrue(issue['important']);self.assertIn('绑定',issue['action'])
        jobs=[{'id':'a'*32,'operation':'archive.start','state':'failed','created':now,'error':'startup failed'}]
        healthy=self.state()
        self.assertEqual(health.report(self.root,{'auto_capture':True},healthy,healthy,jobs,now=now)['important_count'],0)
        # Invalid user destinations remain an in-app correction, not a system alert.
        jobs=[{'id':'b'*32,'operation':'restore','state':'failed','created':now,'error':'restore requires a new destination'}]
        self.assertEqual(health.report(self.root,{'auto_capture':False},None,{},jobs,now=now)['important_count'],0)
        jobs[0]['error']='restored file hash mismatch'
        important=health.report(self.root,{'auto_capture':False},None,{},jobs,now=now)
        self.assertEqual(important['important_count'],1)
        self.assertIsNone(health.next_notice(self.root,important,enabled=False,now=now))
        self.assertFalse(health.notice_path(self.root).exists())

    def test_global_notice_rate_and_bounded_diagnostic_journal(self):
        now=1_000_000
        first={'issues':[{'id':'source:a:integrity','title':'校验失败','summary':'校验未通过','important':True,'severity':'critical','target':'health/a'}]}
        self.assertIsNotNone(health.next_notice(self.root,first,now=now))
        first['issues'][0]['id']='source:b:disk_full'
        self.assertIsNone(health.next_notice(self.root,first,now=now+899))
        self.assertIsNotNone(health.next_notice(self.root,first,now=now+900))
        for n in range(140):health.event(self.root,'retry',count=n)
        events=json.loads((self.root/'ui/health-events.private.json').read_text())['events']
        self.assertEqual(len(events),128);self.assertEqual(events[-1]['sequence'],140)

    def test_read_results_separate_from_polling_and_old_jobs_migrate(self):
        directory=self.root/'ui/jobs';directory.mkdir(parents=True)
        atomic_json(directory/('c'*32+'.json'),{'id':'c'*32,'operation':'messages','state':'completed','created':1,'result':{'large':'x'*2_000_000}})
        jobs=Jobs(Product(self.root))
        try:
            self.assertTrue(jobs.get('c'*32)['result']['large'].startswith('x'))
            self.assertLess((directory/('c'*32+'.json')).stat().st_size,1024)
            self.assertNotIn('result',jobs.list()[0])
            original=Path.read_text;reads=[]
            def read(path,*args,**kwargs):reads.append(path);return original(path,*args,**kwargs)
            with patch.object(Path,'read_text',read):
                jobs.list();job_summaries(self.root)
            self.assertFalse(any(path.parent.name=='results' for path in reads))
        finally:jobs.close()

    def test_important_operation_survives_read_job_retention_and_recovers(self):
        health.outcome(self.root,'backup','failed',finished=100,error='disk full',job_id='a'*32)
        reads=[{'id':str(n),'operation':'messages','state':'completed','finished':n+200} for n in range(60)]
        info=health.report(self.root,{},None,{},reads)
        self.assertEqual(info['important_count'],1);self.assertEqual(info['issues'][0]['code'],'disk_full')
        health.outcome(self.root,'backup','cancelled',finished=300)
        self.assertEqual(health.report(self.root,{},None,{},reads)['important_count'],1)
        health.outcome(self.root,'backup','completed',finished=400)
        health.outcome(self.root,'backup','failed',finished=101,error='older failure')
        self.assertEqual(health.report(self.root,{},None,{},reads)['important_count'],0)
        with patch.object(Path,'mkdir',side_effect=OSError('disk full')):
            self.assertFalse(health.outcome(self.root,'verify','completed',finished=500))

    def test_cancelled_before_start_and_sql_verify_do_not_poison_writer(self):
        with self.assertRaises(InterruptedError):Product(self.root).execute('verify',cancelled=lambda:True)
        self.assertFalse((self.root/'status.private.json').exists())
        from qq_archive_next.catalog import Catalog
        catalog=Catalog(self.root/'catalog.sqlite');cancel=threading.Event()
        try:
            def costly(**kwargs):
                cancel.set()
                catalog.db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
            with patch.object(catalog,'_verify',side_effect=costly):
                with self.assertRaises(InterruptedError):catalog.verify(cancelled=cancel.is_set)
            self.assertEqual(catalog.db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n').fetchone()[0],50005000)
            self.assertEqual(catalog.verify()['raw_versions_verified'],6)
        finally:catalog.close()

    @unittest.skipUnless(os.name=='nt','Windows desktop integration')
    def test_status_publish_waits_for_transient_windows_reader(self):
        path=self.root/'status-reader.json';atomic_json(path,{'value':'before'})
        errors=[]
        def publish():
            try:atomic_json(path,{'value':'after'})
            except Exception as exc:errors.append(str(exc))
        with path.open('r',encoding='utf-8') as reader:
            worker=threading.Thread(target=publish);worker.start();time.sleep(.05)
            self.assertEqual(json.load(reader)['value'],'before');self.assertTrue(worker.is_alive())
        worker.join(2);self.assertFalse(errors);self.assertFalse(worker.is_alive())
        self.assertEqual(json.loads(path.read_text())['value'],'after')

    @unittest.skipUnless(os.name=='nt','Windows desktop integration')
    def test_optional_login_preferences_and_isolated_shortcuts(self):
        import winreg
        app=self.root/'application';core=app/'backend/QQArchiveCore/QQArchiveCore.exe';core.parent.mkdir(parents=True);core.write_bytes(b'fixture')
        key='Software\\QQArchiveNextTests\\'+uuid.uuid4().hex
        try:
            with patch.object(desktop,'RUN_KEY',key):
                self.assertFalse(desktop.settings(self.root)['launch_at_login'])
                enabled=desktop.settings(self.root,{'launch_at_login':True,'start_in_tray':True,'important_notifications':False})
                self.assertTrue(enabled['launch_at_login']);self.assertFalse(enabled['important_notifications'])
                self.assertIn('--login-start',desktop._run_value(self.root))
                self.assertFalse(desktop.settings(self.root,{'launch_at_login':False})['launch_at_login'])
            folder=self.fixture.root/'desktop';folder.mkdir()
            links=desktop.shortcuts(self.root,directory=folder)['shortcuts']
            self.assertEqual(len(links),2);self.assertTrue(any('--tray' in item['arguments'] for item in links))
            self.assertTrue(all(Path(item['target'])==app/'QQArchiveNext.exe' for item in links))
        finally:
            try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,key)
            except FileNotFoundError:pass
            try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,r'Software\QQArchiveNextTests')
            except OSError:pass


class CaptureReliabilityTests(unittest.TestCase):
    def setUp(self):self.fixture=fixtures.OperationsTests('runTest');self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_budget_checked_during_freeze_and_releases_source_guard(self):
        source=Path(self.fixture.config['sources'][0]['database']);shm=Path(str(source)+'-shm');shm.write_bytes(bytes(32768));before=source.read_bytes()
        from qq_archive_next.checkpoint import CheckpointGuard
        with patch.object(CheckpointGuard,'validate',side_effect=AuthenticationError('read-only input freeze exceeded its checkpoint guard budget')):
            with self.assertRaises(AuthenticationError):update(source,self.fixture.state/'views/test/current.sqlite',fixtures.PASSWORD)
        self.assertEqual(source.read_bytes(),before);self.assertFalse((self.fixture.state/'views/test/current.dirty').exists())
        if os.name=='nt':
            import msvcrt
            with shm.open('r+b',buffering=0) as stream:stream.seek(123);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);stream.seek(123);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)

    def test_retry_backoff_survives_restart_and_capture_media_failures_are_separate(self):
        coordinator=Coordinator(self.fixture.state);source=coordinator.config['sources'][0]
        coordinator.failed(source,AuthenticationError('read-only input freeze exceeded its checkpoint guard budget'),'capture')
        coordinator.failed(source,FileNotFoundError('media source missing'),'media');coordinator.save_status()
        after=Coordinator(self.fixture.state)
        self.assertGreater(after.retry_after[source['id']],time.monotonic())
        self.assertGreater(after.media_retry_after[source['id']],time.monotonic())
        catalog=__import__('qq_archive_next.catalog',fromlist=['Catalog']).Catalog(self.fixture.state/'catalog.sqlite')
        try:coordinator.capture(catalog,source)
        finally:catalog.close()
        state=coordinator.status['sources'][source['id']]
        self.assertEqual(state['state'],'ok');self.assertNotIn('capture_issue',state);self.assertIn('media_issue',state)

    def test_startup_failure_has_durable_actionable_reason(self):
        coordinator=Coordinator(self.fixture.state)
        with patch.object(coordinator,'check_version',side_effect=ValueError('QQ client version changed; capture stopped pending a tested new binding')):
            with self.assertRaises(ValueError):coordinator.run()
        status=json.loads((self.fixture.state/'status.private.json').read_text())
        self.assertEqual(status['fatal']['code'],'client_version');self.assertEqual(status['state'],'stopped')


if __name__=='__main__':unittest.main()

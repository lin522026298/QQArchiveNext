from pathlib import Path
import hashlib,json,sqlite3,tempfile,threading,time,unittest
from unittest.mock import patch
import test_qq_unified_archive as fixtures
from qq_archive_next import backup,repository_gc as gc
from qq_archive_next.backup_blocks import Pack
from qq_archive_next.materialize import atomic_json


class RepositoryGcTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.UnifiedArchiveTests();self.fixture.setUp()
        self.base=self.fixture.root;self.repo=self.base/'repository'
    def tearDown(self):self.fixture.tearDown()
    def point(self,number,*,pin=False):
        self.fixture.catalog.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('revision',str(number)))
        self.fixture.catalog.db.commit()
        return backup.create(self.fixture.state,self.repo,pin=pin)['id']
    def rebuild(self):
        destination=self.base/('.repository.compact-'+str(time.time_ns()))
        result=gc.rebuild(self.repo,destination);return destination,result

    def test_rolling_default_keeps_two_successes_and_pinned_point(self):
        first=self.point(1,pin=True);second=self.point(2);third=self.point(3);fourth=self.point(4)
        self.assertEqual(gc.status(self.repo)['keep'],[fourth,third,first])
        self.assertEqual(set(p.stem for p in (self.repo/'points').glob('*.json')),{fourth,third,first})
        self.assertFalse((self.repo/'points'/(second+'.json')).exists())
        backup.verify_point(self.repo,first)

    def test_failed_or_cancelled_backup_never_retires_good_points(self):
        first=self.point(1);second=self.point(2);before=(self.repo/'refs.json').read_bytes()
        with self.assertRaises(InterruptedError):backup.create(self.fixture.state,self.repo,cancelled=lambda:True)
        self.assertEqual(before,(self.repo/'refs.json').read_bytes())
        with patch('qq_archive_next.backup_blocks.Pack.file',side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError):self.point(3)
        self.assertEqual(before,(self.repo/'refs.json').read_bytes())
        for identity in (first,second):backup.verify_point(self.repo,identity)

    def test_rebuild_preserves_descriptors_relationships_all_versions_and_restore(self):
        first=self.point(1,pin=True);self.point(2);self.point(3)
        pack=Pack(self.repo);dead=pack.put(b'UNREFERENCED'*10000);pack.commit();pack.close()
        candidate,result=self.rebuild()
        self.assertEqual(result['pinned'],[first])
        for identity in result['keep']:
            self.assertEqual((self.repo/'points'/(identity+'.json')).read_bytes(),(candidate/'points'/(identity+'.json')).read_bytes())
            backup.verify_point(candidate,identity)
        pack=Pack(candidate,readonly=True)
        self.assertIsNone(pack.db.execute('SELECT 1 FROM block WHERE id=?',(dead['id'],)).fetchone());pack.close()
        receipt=backup.restore(candidate,self.base/'restored',identity=first,hardlink_immutable=True)
        self.assertTrue(receipt['deep_semantic_verification']);self.assertTrue(receipt['capture_disabled'])
        self.assertEqual(receipt['verification']['logical_messages'],3)
        switched=gc.switch(self.repo,candidate)
        backup.verify_point(self.repo)
        gc.discard_old(self.repo,switched['old']);self.assertFalse(Path(switched['old']).exists())

    def test_source_advancement_refuses_switch(self):
        self.point(1);candidate,_=self.rebuild();self.point(2)
        with self.assertRaisesRegex(ValueError,'advanced'):gc.switch(self.repo,candidate)
        backup.verify_point(self.repo)

    def test_interrupted_switch_rolls_back_each_precommit_phase(self):
        self.point(1)
        for phase in ('ready','old_moved','new_active'):
            candidate,_=self.rebuild();before=(self.repo/'refs.json').read_bytes()
            def crash(current):
                if current==phase:raise OSError('simulated crash')
            with self.assertRaises(OSError):gc.switch(self.repo,candidate,checkpoint=crash)
            self.assertEqual(gc.recover(self.repo)['state'],'rolled_back')
            self.assertEqual((self.repo/'refs.json').read_bytes(),before);backup.verify_point(self.repo)

    def test_interrupted_committed_switch_retains_new_and_old(self):
        self.point(1);candidate,_=self.rebuild()
        def crash(phase):
            if phase=='committed':raise OSError('simulated crash')
        with self.assertRaises(OSError):gc.switch(self.repo,candidate,checkpoint=crash)
        recovered=gc.recover(self.repo);self.assertEqual(recovered['state'],'committed')
        self.assertTrue(Path(recovered['old']).exists());backup.verify_point(self.repo)

    def test_corrupt_reachable_payload_or_missing_pin_does_not_touch_original(self):
        first=self.point(1);before=(self.repo/'refs.json').read_bytes()
        pack=Pack(self.repo);row=pack.db.execute('SELECT id FROM block WHERE kind=0 LIMIT 1').fetchone();pack.db.execute('DROP TRIGGER block_no_update')
        pack.db.execute('UPDATE block SET data=? WHERE id=?',(b'corrupt',row[0]));pack.commit();pack.close()
        with self.assertRaises(Exception):self.rebuild()
        self.assertEqual((self.repo/'refs.json').read_bytes(),before)
        refs=json.loads(before);refs['pinned']=['f'*64];atomic_json(self.repo/'refs.json',refs)
        with self.assertRaises(FileNotFoundError):gc.retain(self.repo)
        self.assertTrue((self.repo/'points'/(first+'.json')).exists())

    def test_read_lease_blocks_replacement_and_can_be_cancelled(self):
        self.point(1);started=threading.Event();stop=threading.Event();result=[]
        def writer():
            started.set()
            try:
                with gc.lease(self.repo,write=True,cancelled=stop.is_set):result.append('acquired')
            except InterruptedError:result.append('cancelled')
        with gc.lease(self.repo):
            thread=threading.Thread(target=writer);thread.start();started.wait();time.sleep(.15);stop.set();thread.join(2)
        self.assertEqual(result,['cancelled'])

    def test_existing_repository_requires_explicit_verified_adoption(self):
        self.point(1);(self.repo/'RETENTION.json').unlink()
        for number in range(2,5):self.point(number)
        self.assertEqual(len(list((self.repo/'points').glob('*.json'))),4)
        self.assertFalse(gc.status(self.repo)['rolling_enabled'])
        gc.compact(self.repo)
        self.assertTrue(gc.status(self.repo)['rolling_enabled']);self.assertEqual(gc.status(self.repo)['points'],2)

    def test_cleanup_rejects_unrecognized_files_before_deleting_anything(self):
        self.point(1);candidate,_=self.rebuild();switched=gc.switch(self.repo,candidate)
        old=Path(switched['old']);(old/'personal-note.txt').write_text('preserve')
        with self.assertRaisesRegex(ValueError,'unrecognized'):gc.discard_old(self.repo,old)
        self.assertTrue((old/'blocks.sqlite').exists());self.assertTrue((old/'personal-note.txt').exists())

    def test_failed_compaction_cleans_only_the_new_build(self):
        self.point(1);before=(self.repo/'refs.json').read_bytes()
        with patch('qq_archive_next.backup.verify_point',side_effect=ValueError('injected verification failure')):
            with self.assertRaises(ValueError):gc.compact(self.repo)
        self.assertEqual((self.repo/'refs.json').read_bytes(),before)
        self.assertEqual(list(self.base.glob('.repository.compact-*')),[])
        backup.verify_point(self.repo)

    def test_weekly_automatic_compaction_and_new_backup_after_switch(self):
        self.point(1,pin=True);self.point(2)
        policy=gc.policy(self.repo);policy['last_compaction']=time.time()-8*86400;atomic_json(self.repo/'RETENTION.json',policy)
        self.point(3);self.point(4)
        # The fourth successful point retires the first non-pinned point.
        self.assertTrue((self.repo/'COMPACTION_RECEIPT.json').exists())
        self.point(5);self.assertEqual(gc.status(self.repo)['points'],3);backup.verify_point(self.repo)

    def test_legacy_external_slices_become_independent_blocks(self):
        from qq_archive_next.backup import repository,store_file
        # Seed a pack directly, then make a valid v2 file map around a legacy slice.
        repo=repository(self.repo,create=True);path=self.base/'sample.sqlite';path.write_bytes(b'a'*8192)
        entry,_=store_file(repo,path,lambda:False);pack=Pack(repo)
        pack.seed_legacy({'id':'a'*64,'body':{'files':{'sample.sqlite':entry}}})
        tree=pack.file_tree({'sample.sqlite':pack.file(path,block_size=4096)});pack.commit();pack.close()
        body={'schema':backup.SCHEMA,'files':tree}
        from qq_archive_next.catalog import canonical
        identity=hashlib.sha256(canonical(body).encode()).hexdigest();atomic_json(repo/'points'/(identity+'.json'),{'id':identity,'created':time.time(),'body':body})
        atomic_json(repo/'refs.json',{'schema':'qq-next-backup-refs/1','recent':[identity],'pinned':[]})
        candidate,_=self.rebuild();self.assertFalse((candidate/'chunks').exists());backup.verify_point(candidate,identity)


if __name__=='__main__':unittest.main()

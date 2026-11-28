from pathlib import Path
import hashlib,os,sqlite3,tempfile,unittest
from contextlib import closing
from unittest.mock import patch
from qq_archive_next.materialize import update
from qq_archive_next.cipher import AuthenticationError
from qq_archive_next.wal import freeze,verify_live_prefix,STRIDE
from qq_next_fixture import PASSWORD,native,wrap,encrypted_wal
from qq_archive_next.cipher import PAGE,WRAPPER


class AuthenticatedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.plain=self.root/'plain.sqlite';native(self.plain)
        self.encrypted=self.root/'nt_msg.db';wrap(self.plain,self.encrypted)
        self.view=self.root/'owned/current.sqlite'
    def test_authenticated_roundtrip_and_idle_zero_page_writes(self):
        first=update(self.encrypted,self.view,PASSWORD)
        db=sqlite3.connect(self.view)
        self.assertEqual(db.execute('SELECT value FROM evidence WHERE id=1').fetchone()[0],bytes(range(256))*32);db.close()
        before=self.view.read_bytes()
        second=update(self.encrypted,self.view,PASSWORD)
        self.assertEqual(second['changed_pages'],0)
        self.assertEqual(second['decrypted_pages'],0)
        self.assertEqual(second['snapshot_bytes'],0)
        self.assertEqual(second['frozen_pages'],0)
        self.assertEqual(before,self.view.read_bytes())
    def test_committed_wal_backfill_and_late_update(self):
        db=sqlite3.connect(self.plain);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        # Refresh the base after switching its header to WAL mode.
        wrap(self.plain,self.encrypted)
        db.execute("INSERT INTO evidence VALUES(-7,'late old key')");db.commit()
        db.execute("UPDATE evidence SET value='edited' WHERE id=1");db.commit()
        wal=Path(str(self.encrypted)+'-wal')
        wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
        report=update(self.encrypted,self.view,PASSWORD)
        self.assertGreater(report['wal_committed_frames'],0)
        result=sqlite3.connect(self.view)
        self.assertEqual(result.execute('SELECT id,value FROM evidence ORDER BY id').fetchall(),[(-7,'late old key'),(1,'edited')])
        result.close();db.close()
    def test_wrong_key_and_corruption_refused(self):
        with self.assertRaises(AuthenticationError):update(self.encrypted,self.view,b'x'*16)
        data=bytearray(self.encrypted.read_bytes());data[1100]^=1;self.encrypted.write_bytes(data)
        with self.assertRaises(AuthenticationError):update(self.encrypted,self.view,PASSWORD)

    def test_hot_rollback_journal_is_not_silently_ignored(self):
        journal=Path(str(self.encrypted)+'-journal');journal.write_bytes(bytes.fromhex('d9d505f920a163d7'))
        with self.assertRaisesRegex(AuthenticationError,'source-owner recovery'):update(self.encrypted,self.view,PASSWORD)
        self.assertFalse(self.view.exists())
    def test_dirty_failure_forces_rebuild_and_tampered_view_is_repaired(self):
        def fail(stage):raise RuntimeError('synthetic crash')
        with self.assertRaises(RuntimeError):update(self.encrypted,self.view,PASSWORD,fault=fail)
        self.assertTrue(self.view.with_suffix('.dirty').exists())
        update(self.encrypted,self.view,PASSWORD)
        self.assertFalse(self.view.with_suffix('.dirty').exists())
        data=bytearray(self.view.read_bytes());data[150]^=1;self.view.write_bytes(data)
        repaired=update(self.encrypted,self.view,PASSWORD)
        self.assertGreater(repaired['changed_pages'],0)
    def test_source_change_after_owned_cut_does_not_invalidate_snapshot(self):
        done=[False]
        def change(stage):
            if not done[0]:
                with self.encrypted.open('r+b') as f:f.seek(15);f.write(b'X')
                done[0]=True
        report=update(self.encrypted,self.view,PASSWORD,fault=change)
        self.assertFalse(self.view.with_suffix('.dirty').exists())
        self.assertGreater(report['frozen_pages'],0)
        db=sqlite3.connect(self.view)
        self.assertEqual(db.execute('SELECT value FROM evidence WHERE id=1').fetchone()[0],bytes(range(256))*32);db.close()

    def test_failed_input_cut_preserves_the_previously_clean_owned_view(self):
        update(self.encrypted,self.view,PASSWORD)
        self.view.with_suffix('.pending').unlink();before=self.view.read_bytes()
        def corrupt_before_cut(path,**kwargs):
            frozen=freeze(path,**kwargs)
            with self.encrypted.open('r+b') as stream:
                stream.seek(WRAPPER+PAGE+100);value=stream.read(1);stream.seek(-1,1);stream.write(bytes([value[0]^1]))
            return frozen
        with patch('qq_archive_next.materialize.freeze',side_effect=corrupt_before_cut):
            with self.assertRaisesRegex(AuthenticationError,'outside frozen'):update(self.encrypted,self.view,PASSWORD)
        self.assertEqual(before,self.view.read_bytes())
        self.assertFalse(self.view.with_suffix('.dirty').exists())

    def test_corrupt_owned_snapshot_cannot_bypass_authentication(self):
        constructor=tempfile.SpooledTemporaryFile
        class Corrupted:
            def __init__(self,stream):self.stream=stream;self.corrupted=False
            def __getattr__(self,name):return getattr(self.stream,name)
            def seek(self,offset,whence=0):
                if offset==0 and whence==0 and not self.corrupted:
                    self.stream.seek(40);value=self.stream.read(1);self.stream.seek(40);self.stream.write(bytes([value[0]^1]));self.corrupted=True
                return self.stream.seek(offset,whence)
        def temporary(*args,**kwargs):
            from qq_archive_next.materialize import SNAPSHOT_MEMORY
            stream=constructor(*args,**kwargs)
            return Corrupted(stream) if kwargs.get('max_size')==SNAPSHOT_MEMORY else stream
        with patch('qq_archive_next.materialize.tempfile.SpooledTemporaryFile',side_effect=temporary):
            with self.assertRaisesRegex(AuthenticationError,'owned snapshot bytes changed'):update(self.encrypted,self.view,PASSWORD)
        self.assertTrue(self.view.with_suffix('.dirty').exists())
        self.assertFalse(self.view.with_suffix('.pending').exists())

    def test_wal_reset_corrupt_complete_tail_and_partial_tail(self):
        db=sqlite3.connect(self.plain);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO evidence VALUES(-9,'committed')");db.commit()
        data=encrypted_wal(Path(str(self.plain)+'-wal').read_bytes())
        wal=Path(str(self.encrypted)+'-wal');wal.write_bytes(data+b'partial frame')
        frozen=freeze(wal,memory_bytes=1)
        try:
            self.assertGreater(frozen.committed_frames,0);verify_live_prefix(wal,frozen)
            with self.assertRaises(InterruptedError):verify_live_prefix(wal,frozen,cancelled=lambda:True)
            changed=bytearray(data);changed[16]^=1;wal.write_bytes(changed)
            with self.assertRaises(AuthenticationError):verify_live_prefix(wal,frozen)
        finally:frozen.close()
        changed=bytearray(data);changed[-1]^=1;wal.write_bytes(changed)
        with self.assertRaisesRegex(AuthenticationError,'checksum'):freeze(wal)
        wal.write_bytes(data)
        with self.assertRaises(InterruptedError):freeze(wal,cancelled=lambda:True)
        db.close()

    def test_pending_reconciliation_is_never_narrowed_after_a_crash(self):
        update(self.encrypted,self.view,PASSWORD)
        self.view.with_suffix('.pending').unlink()
        db=sqlite3.connect(self.plain);db.execute("UPDATE evidence SET value='modified' WHERE id=1");db.commit();db.close()
        wrap(self.plain,self.encrypted)
        first=update(self.encrypted,self.view,PASSWORD,deep=False)
        self.assertTrue(first['needs_scan'])
        second=update(self.encrypted,self.view,PASSWORD,deep=False)
        self.assertIsNone(second['row_windows']);self.assertTrue(second['needs_scan'])

    def test_checkpoint_changes_covered_by_frozen_wal_are_safe(self):
        db=sqlite3.connect(self.plain);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        wrap(self.plain,self.encrypted)
        db.execute('UPDATE evidence SET value=? WHERE id=1',(b'q'*8192,));db.commit()
        wal=Path(str(self.encrypted)+'-wal');wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
        from qq_archive_next.materialize import baseline_main
        def checkpoint(*args,**kwargs):
            proof=baseline_main(*args,**kwargs)
            frozen=freeze(wal)
            try:
                with self.encrypted.open('r+b') as stream:
                    for number,page in frozen.pages():stream.seek(WRAPPER+(number-1)*PAGE);stream.write(page)
            finally:frozen.close()
            return proof
        try:
            with patch('qq_archive_next.materialize.baseline_main',side_effect=checkpoint):
                report=update(self.encrypted,self.view,PASSWORD)
        finally:db.close()
        self.assertGreater(report['main_changes_covered_by_wal'],0)
        con=sqlite3.connect(self.view)
        self.assertEqual(con.execute('SELECT value FROM evidence').fetchone()[0],b'q'*8192)
        con.close()

    def test_main_change_outside_committed_wal_still_refuses_capture(self):
        db=sqlite3.connect(self.plain)
        db.execute('CREATE TABLE independent(id INTEGER PRIMARY KEY,value TEXT)');db.execute("INSERT INTO independent VALUES(1,'untouched')");db.commit()
        root=db.execute("SELECT rootpage FROM sqlite_master WHERE name='independent'").fetchone()[0]
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0');wrap(self.plain,self.encrypted)
        db.execute('UPDATE evidence SET value=? WHERE id=1',(b'new committed payload'*400,));db.commit()
        wal=Path(str(self.encrypted)+'-wal');wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
        from qq_archive_next.materialize import baseline_main
        def corrupt_uncovered(*args,**kwargs):
            proof=baseline_main(*args,**kwargs)
            frozen=freeze(wal)
            try:self.assertNotIn(root,[number for number,page in frozen.pages()])
            finally:frozen.close()
            with self.encrypted.open('r+b') as stream:
                stream.seek(WRAPPER+(root-1)*PAGE+100);byte=stream.read(1);stream.seek(-1,1);stream.write(bytes([byte[0]^1]))
            return proof
        with patch('qq_archive_next.materialize.baseline_main',side_effect=corrupt_uncovered):
            with self.assertRaisesRegex(AuthenticationError,'outside frozen'):update(self.encrypted,self.view,PASSWORD)
        db.close()

    def _wal_update(self):
        db=sqlite3.connect(self.plain)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        wrap(self.plain,self.encrypted)
        update(self.encrypted,self.view,PASSWORD)
        self.view.with_suffix('.pending').unlink()
        db.execute("UPDATE evidence SET value='first commit' WHERE id=1")
        db.commit()
        wal=Path(str(self.encrypted)+'-wal')
        wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
        return db,wal

    def test_append_after_prefix_proofs_keeps_the_selected_commit(self):
        db,wal=self._wal_update()
        db.execute("UPDATE evidence SET value='later commit' WHERE id=1")
        db.commit()
        appended=encrypted_wal(Path(str(self.plain)+'-wal').read_bytes())
        prior=wal.read_bytes()
        self.assertTrue(appended.startswith(prior))

        def append_before_seal(path,expected,*,commit=None):
            from qq_archive_next.wal import verify_epoch
            if commit is not None:
                with wal.open('ab') as out:
                    out.write(appended[len(prior):])
            return verify_epoch(path,expected,commit=commit)

        with patch('qq_archive_next.materialize.verify_epoch',side_effect=append_before_seal), \
             patch('qq_archive_next.materialize.MAX_WAL_INDEX_FRAMES',0):
            proof=update(self.encrypted,self.view,PASSWORD)
        self.assertTrue(proof['wal_overlay_spilled'])
        self.assertEqual(proof['input_cut_strategy'],'wal_epoch_short_guard')
        with closing(sqlite3.connect(self.view)) as check:
            self.assertEqual(check.execute('SELECT value FROM evidence').fetchone()[0],'first commit')
        update(self.encrypted,self.view,PASSWORD)
        with closing(sqlite3.connect(self.view)) as check:
            self.assertEqual(check.execute('SELECT value FROM evidence').fetchone()[0],'later commit')

    def test_replaced_wal_after_prefix_proofs_preserves_view_and_index(self):
        _,wal=self._wal_update()
        owned=[self.view,self.view.with_suffix('.pages.sqlite')]
        before=[hashlib.sha256(path.read_bytes()).digest() for path in owned]

        def replace_before_seal(path,expected,*,commit=None):
            from qq_archive_next.wal import verify_epoch
            if commit is not None:
                replacement=wal.with_suffix('.replacement')
                replacement.write_bytes(wal.read_bytes())
                replacement.replace(wal)
            return verify_epoch(path,expected,commit=commit)

        with patch('qq_archive_next.materialize.verify_epoch',side_effect=replace_before_seal):
            with self.assertRaisesRegex(AuthenticationError,'WAL reset'):
                update(self.encrypted,self.view,PASSWORD)
        self.assertEqual(before,[hashlib.sha256(path.read_bytes()).digest() for path in owned])
        self.assertFalse(self.view.with_suffix('.dirty').exists())
        self.assertFalse(self.view.with_suffix('.pending').exists())

    def test_late_wal_growth_is_covered_by_the_new_cut(self):
        from qq_archive_next.materialize import baseline_main
        db,wal=self._wal_update()
        old_size=self.encrypted.stat().st_size
        def grow_between_main_reads(*args,**kwargs):
            proof=baseline_main(*args,**kwargs)
            db.execute('UPDATE evidence SET value=? WHERE id=1',(b'q'*65536,))
            db.commit()
            wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
            with closing(freeze(wal)) as frozen,self.encrypted.open('r+b') as out:
                for number,page in frozen.pages():
                    out.seek(WRAPPER+(number-1)*PAGE)
                    out.write(page)
            return proof
        with patch('qq_archive_next.materialize.baseline_main',side_effect=grow_between_main_reads):
            proof=update(self.encrypted,self.view,PASSWORD)
        self.assertGreater(self.encrypted.stat().st_size,old_size)
        self.assertGreater(proof['main_changes_covered_by_wal'],0)
        with closing(sqlite3.connect(self.view)) as check:
            self.assertEqual(check.execute('SELECT value FROM evidence').fetchone()[0],b'q'*65536)

    @unittest.skipUnless(os.name=='nt','Windows WAL locking protocol')
    def test_same_file_wal_restart_uses_guarded_block_repair(self):
        from qq_archive_next.materialize import verify_main
        db,wal=self._wal_update()
        first_header=wal.read_bytes()[:32]
        Path(str(self.encrypted)+'-shm').write_bytes(bytes(32768))
        observed=False
        def checkpoint_after_bulk_scan(*args,**kwargs):
            nonlocal observed
            if not observed:
                observed=True
                self.assertEqual(db.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0],0)
                wrap(self.plain,self.encrypted)
                db.execute("UPDATE evidence SET value='new epoch' WHERE id=1")
                db.commit()
                wal.write_bytes(encrypted_wal(Path(str(self.plain)+'-wal').read_bytes()))
                self.assertNotEqual(first_header,wal.read_bytes()[:32])
            return verify_main(*args,**kwargs)
        with patch('qq_archive_next.materialize.verify_main',side_effect=checkpoint_after_bulk_scan):
            proof=update(self.encrypted,self.view,PASSWORD)
        self.assertEqual(proof['input_cut_strategy'],'wal_guarded_recheck')
        self.assertGreater(proof['repair_bytes'],0)
        with closing(sqlite3.connect(self.view)) as check:
            self.assertEqual(check.execute('SELECT value FROM evidence').fetchone()[0],'new epoch')

    @unittest.skipUnless(os.name=='nt','Windows WAL locking protocol')
    def test_checkpoint_after_bulk_reads_rechecks_staged_bytes(self):
        _,wal=self._wal_update()
        Path(str(self.encrypted)+'-shm').write_bytes(bytes(32768))
        checked=0
        def checkpoint_at_seal(path,frozen,**kwargs):
            nonlocal checked
            verify_live_prefix(path,frozen,**kwargs)
            checked+=1
            if checked==2:
                with self.encrypted.open('r+b') as out:
                    for number,page in frozen.pages():
                        out.seek(WRAPPER+(number-1)*PAGE)
                        out.write(page)
                stat=self.encrypted.stat()
                os.utime(self.encrypted,ns=(stat.st_atime_ns,stat.st_mtime_ns+100000000))
        with patch('qq_archive_next.materialize.verify_live_prefix',side_effect=checkpoint_at_seal):
            proof=update(self.encrypted,self.view,PASSWORD)
        self.assertEqual(proof['input_cut_strategy'],'wal_guarded_recheck')
        self.assertGreater(proof['repair_bytes'],0)
        with closing(sqlite3.connect(self.view)) as check:
            self.assertEqual(check.execute('SELECT value FROM evidence').fetchone()[0],'first commit')

    @unittest.skipUnless(os.name=='nt','Windows WAL locking protocol')
    def test_bulk_reads_are_unlocked_and_short_guard_timeout_leaves_clean_view(self):
        import msvcrt
        from qq_archive_next.checkpoint import CheckpointGuard
        from qq_archive_next.materialize import baseline_main
        self._wal_update()
        shm=Path(str(self.encrypted)+'-shm')
        shm.write_bytes(bytes(32768))
        before=self.view.read_bytes()
        validate=CheckpointGuard.validate

        def assert_unlocked():
            with shm.open('r+b',buffering=0) as probe:
                probe.seek(123)
                msvcrt.locking(probe.fileno(),msvcrt.LK_NBLCK,1)
                probe.seek(123)
                msvcrt.locking(probe.fileno(),msvcrt.LK_UNLCK,1)

        def bulk_baseline(*args,**kwargs):
            assert_unlocked()
            return baseline_main(*args,**kwargs)

        def bulk_prefix(*args,**kwargs):
            assert_unlocked()
            return verify_live_prefix(*args,**kwargs)

        def time_out(guard):
            self.assertEqual(guard.mode,'win32_sqlite_read0')
            guard.started-=31
            validate(guard)

        with patch('qq_archive_next.materialize.baseline_main',side_effect=bulk_baseline), \
             patch('qq_archive_next.materialize.verify_live_prefix',side_effect=bulk_prefix), \
             patch.object(CheckpointGuard,'validate',time_out):
            with self.assertRaisesRegex(AuthenticationError,'guard budget'):
                update(self.encrypted,self.view,PASSWORD)
        assert_unlocked()
        self.assertEqual(before,self.view.read_bytes())
        self.assertFalse(self.view.with_suffix('.dirty').exists())
        self.assertFalse(self.view.with_suffix('.pending').exists())


if __name__=='__main__':unittest.main()

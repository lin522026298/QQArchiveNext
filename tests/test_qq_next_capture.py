from pathlib import Path
import hashlib,sqlite3,tempfile,unittest
from unittest.mock import patch
from qq_archive_next.materialize import update
from qq_archive_next.cipher import AuthenticationError
from qq_archive_next.wal import freeze,verify_live_prefix,STRIDE
from qq_next_fixture import PASSWORD,native,wrap,encrypted_wal
from qq_archive_next.cipher import PAGE,WRAPPER


class AuthenticatedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.plain=self.root/'plain.sqlite';native(self.plain)
        self.encrypted=self.root/'nt_msg.db';wrap(self.plain,self.encrypted)
        self.view=self.root/'owned/current.sqlite'
    def tearDown(self):self.temp.cleanup()
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

    def test_corrupt_owned_snapshot_flags_cannot_bypass_authentication(self):
        constructor=tempfile.SpooledTemporaryFile
        class Corrupted:
            def __init__(self,stream):self.stream=stream;self.corrupted=False
            def __getattr__(self,name):return getattr(self.stream,name)
            def seek(self,offset,whence=0):
                if offset==0 and whence==0 and not self.corrupted:
                    self.stream.seek(40);value=self.stream.read(1);self.stream.seek(40);self.stream.write(bytes([value[0]^1]));self.corrupted=True
                return self.stream.seek(offset,whence)
        def temporary(*args,**kwargs):
            stream=constructor(*args,**kwargs)
            return Corrupted(stream) if kwargs.get('dir')==self.view.parent else stream
        with patch('qq_archive_next.materialize.tempfile.SpooledTemporaryFile',side_effect=temporary):
            with self.assertRaisesRegex(AuthenticationError,'snapshot page flags'):update(self.encrypted,self.view,PASSWORD)
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
        def checkpoint(path,**kwargs):
            frozen=freeze(path,**kwargs)
            with self.encrypted.open('r+b') as stream:
                for number,page in frozen.pages():stream.seek(WRAPPER+(number-1)*PAGE);stream.write(page)
            return frozen
        try:
            with patch('qq_archive_next.materialize.freeze',side_effect=checkpoint):
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
        def corrupt_uncovered(path,**kwargs):
            frozen=freeze(path,**kwargs)
            self.assertNotIn(root,[number for number,page in frozen.pages()])
            with self.encrypted.open('r+b') as stream:
                stream.seek(WRAPPER+(root-1)*PAGE+100);byte=stream.read(1);stream.seek(-1,1);stream.write(bytes([byte[0]^1]))
            return frozen
        with patch('qq_archive_next.materialize.freeze',side_effect=corrupt_uncovered):
            with self.assertRaisesRegex(AuthenticationError,'outside frozen'):update(self.encrypted,self.view,PASSWORD)
        db.close()


if __name__=='__main__':unittest.main()

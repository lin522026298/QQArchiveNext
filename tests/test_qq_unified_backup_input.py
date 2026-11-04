from pathlib import Path
import sqlite3
import tempfile
import unittest
import test_qq_unified_archive as fixtures
from qq_archive_next.backup_input import owned_input
from qq_archive_next import backup


class OwnedBackupInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.UnifiedArchiveTests();self.fixture.setUp()
        self.root=self.fixture.state;self.staging=self.fixture.root/'staging';self.staging.mkdir()
    def tearDown(self):self.fixture.tearDown()

    def test_checkpoint_main_is_complete_and_excludes_another_writer(self):
        with owned_input(self.root,'catalog.sqlite',self.staging) as (path,report):
            self.assertEqual(path,self.root/'catalog.sqlite');self.assertEqual(report['temporary_bytes'],0)
            self.assertEqual(report['wal_frames'],report['backfilled_frames'])
            db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
            try:self.assertEqual(db.execute('SELECT count(*) FROM current_message').fetchone()[0],3)
            finally:db.close()
            other=sqlite3.connect(path,timeout=.01)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):other.execute("INSERT INTO metadata VALUES('accidental-writer','blocked')")
            finally:other.close()
        self.assertFalse(list(self.staging.iterdir()))
        self.fixture.catalog.db.execute("INSERT INTO metadata VALUES('after-fence','allowed')");self.fixture.catalog.db.commit()

    def test_older_reader_falls_back_to_complete_online_snapshot(self):
        original=self.root/'catalog.sqlite';reader=sqlite3.connect(original)
        reader.execute('BEGIN');reader.execute('SELECT count(*) FROM current_message').fetchone()
        self.fixture.catalog.db.execute("INSERT INTO metadata VALUES('newly-committed','latest')");self.fixture.catalog.db.commit()
        try:
            with owned_input(self.root,'catalog.sqlite',self.staging) as (path,report):
                self.assertEqual(path,self.staging/'catalog.sqlite');self.assertGreater(report['temporary_bytes'],0)
                self.assertGreater(report['wal_frames'],report['backfilled_frames'])
                db=sqlite3.connect(path)
                try:self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='newly-committed'").fetchone()[0],'latest')
                finally:db.close()
        finally:reader.close()
        result=backup.create(self.root,self.fixture.root/'repository')
        self.assertEqual(result['temporary_snapshot_bytes'],0)
        receipt=backup.restore(self.fixture.root/'repository',self.fixture.root/'restore')
        self.assertTrue(receipt['deep_semantic_verification'])

    def test_foreign_history_path_and_cancellation_are_refused(self):
        with self.assertRaises(ValueError):
            with owned_input(self.root,'history/v2/accounts/legacy/structured/nt_msg_export.db',self.staging):pass
        with self.assertRaises(InterruptedError):
            with owned_input(self.root,'catalog.sqlite',self.staging,lambda:True):pass
        self.assertFalse(list(self.staging.iterdir()))

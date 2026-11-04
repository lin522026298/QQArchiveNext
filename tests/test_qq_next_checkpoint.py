from pathlib import Path
import hashlib
import os
import tempfile
import unittest
from qq_archive_next.checkpoint import CheckpointGuard


@unittest.skipUnless(os.name=='nt','Windows WAL locking protocol')
class CheckpointGuardTests(unittest.TestCase):
    def test_shared_readers_exclude_checkpoint_lock_without_changing_bytes(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'native.db';source.touch();shm=Path(str(source)+'-shm');shm.write_bytes(bytes(32768))
            before=shm.stat();digest=hashlib.sha256(shm.read_bytes()).digest()
            first=CheckpointGuard(source);second=CheckpointGuard(source)
            try:
                self.assertEqual(first.mode,'win32_sqlite_read0')
                with shm.open('r+b',buffering=0) as checkpoint:
                    # WAL_WRITE_LOCK is byte 120, independent of READ_LOCK(0).
                    checkpoint.seek(120);msvcrt.locking(checkpoint.fileno(),msvcrt.LK_NBLCK,1)
                    checkpoint.seek(120);msvcrt.locking(checkpoint.fileno(),msvcrt.LK_UNLCK,1)
                    checkpoint.seek(123)
                    with self.assertRaises(OSError):msvcrt.locking(checkpoint.fileno(),msvcrt.LK_NBLCK,1)
                    first.release();second.validate()
                    with self.assertRaises(OSError):msvcrt.locking(checkpoint.fileno(),msvcrt.LK_NBLCK,1)
                    second.release();msvcrt.locking(checkpoint.fileno(),msvcrt.LK_NBLCK,1)
                    checkpoint.seek(123);msvcrt.locking(checkpoint.fileno(),msvcrt.LK_UNLCK,1)
                self.assertEqual(shm.stat().st_mtime_ns,before.st_mtime_ns)
                self.assertEqual(hashlib.sha256(shm.read_bytes()).digest(),digest)
            finally:first.release();second.release()

    def test_missing_shm_does_not_create_a_source_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'native.db';source.touch();guard=CheckpointGuard(source)
            try:self.assertEqual(guard.mode,'optimistic_no_shm');guard.validate()
            finally:guard.release()
            self.assertFalse(Path(str(source)+'-shm').exists())

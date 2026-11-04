"""Avoid a full temporary copy when an owned SQLite main file is stable.

The caller already holds WriterLock. A reserved SQLite writer transaction also
excludes an accidental second SQL writer. A separate PASSIVE checkpoint must
backfill every committed WAL frame; an older reader triggers online backup.
This is never used on QQ inputs or frozen historical databases.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .config import load


@contextmanager
def owned_input(root,name,staging,cancelled=lambda:False):
    from .backup import sqlite_snapshot
    from .catalog import SCHEMA as CATALOG_SCHEMA
    from .media import SCHEMA as MEDIA_SCHEMA
    root,_=load(root)
    schemas={'catalog.sqlite':CATALOG_SCHEMA,'assets.sqlite':MEDIA_SCHEMA}
    if name not in schemas:raise ValueError('stable backup reads are limited to owned live catalogs')
    path=root/name
    if cancelled():raise InterruptedError('backup cancelled')
    fence=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)
    checkpoint=None
    try:
        fence.execute('BEGIN IMMEDIATE')
        row=fence.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not row or row[0]!=schemas[name]:raise ValueError('refusing to checkpoint a foreign database')
        journal=fence.execute('PRAGMA journal_mode').fetchone()[0]
        complete=journal!='wal';frames=backfilled=0
        if journal=='wal':
            checkpoint=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1)
            busy,frames,backfilled=checkpoint.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            complete=not busy and frames==backfilled
        if cancelled():raise InterruptedError('backup cancelled')
        if complete:
            before=path.stat()
            yield path,{'mode':'reserved_checkpoint_main','temporary_bytes':0,'wal_frames':frames,'backfilled_frames':backfilled}
            after=path.stat()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('reserved backup input changed')
        else:
            target=Path(staging)/name;sqlite_snapshot(path,target,cancelled,check=False)
            yield target,{'mode':'online_snapshot_reader_fallback','temporary_bytes':target.stat().st_size,'wal_frames':frames,'backfilled_frames':backfilled}
    finally:
        if checkpoint:checkpoint.close()
        fence.rollback();fence.close()

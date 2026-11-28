"""Stable-main/two-prefix capture with one rebuildable decoded view.

For an identified WAL epoch, read the main twice before selecting the WAL cut.
The checkpoint lock covers only the final identity and commit witness. Without
an epoch, keep the conservative guard across the two reads. Every uncovered
main page must match both reads, and the WAL prefix is verified twice.
Only necessary encrypted pages enter the owned spool. Dirty/pending markers
protect view writes and catalog acceptance.
"""
from __future__ import annotations

import hashlib
from heapq import merge
import json
import os
import sqlite3
import struct
import time
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path

from .cipher import BINDING, PAGE, WRAPPER, PageCipher, AuthenticationError
from .wal import epoch, freeze, verify_epoch, verify_live_prefix, STRIDE

BASELINE_MEMORY = 8 * 1024**2
SNAPSHOT_MEMORY = 16 * 1024**2
MAX_DELTA_PAGES = 65536
MAX_WAL_INDEX_FRAMES = 65536
MAIN_RECORD = struct.Struct('<I32s')
PAGE_NUMBER = struct.Struct('<I')
LOCK_PAGE = (0x40000000-WRAPPER)//PAGE+1
MAIN_BLOCK = 1024**2
MAX_REPAIR_BYTES = 128*1024**2
MAX_GUARDED_WAL = 16*1024**2
REPAIR_HEADER = struct.Struct('<QI32s')


class WalOverlay:
    """Latest frame offsets, with a disk-backed index for unusually large WALs."""

    def __init__(self, connection, frames):
        self.highest = 0
        self.offsets = {}
        self.connection = connection if frames > MAX_WAL_INDEX_FRAMES else None
        if self.connection is not None:
            connection.executescript("""PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-4096;
                CREATE TEMP TABLE capture_wal_overlay(number INTEGER PRIMARY KEY, offset INTEGER NOT NULL);""")

    def add(self, number, offset):
        self.highest = max(self.highest, number)
        if self.connection is None:
            self.offsets[number] = offset
        else:
            self.connection.execute('INSERT OR REPLACE INTO capture_wal_overlay VALUES(?,?)', (number, offset))

    def shrink(self, pages):
        if pages >= self.highest:
            return
        self.highest = pages
        if self.connection is None:
            self.offsets = {number: offset for number, offset in self.offsets.items() if number <= pages}
        else:
            self.connection.execute('DELETE FROM capture_wal_overlay WHERE number>?', (pages,))

    def items(self):
        if self.connection is None:
            return iter(sorted(self.offsets.items()))
        return self.connection.execute('SELECT number,offset FROM capture_wal_overlay ORDER BY number')

    def contains(self, number):
        if self.connection is None:
            return number in self.offsets
        return self.connection.execute('SELECT 1 FROM capture_wal_overlay WHERE number=?', (number,)).fetchone() is not None


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        deadline=time.monotonic()+1
        while True:
            try:os.replace(temporary,path);break
            except PermissionError as exc:
                # A short-lived Windows status reader can deny replacement.
                # Never truncate the published file or retry a real write error.
                if os.name!='nt' or exc.winerror not in (5,32,33) or time.monotonic()>=deadline:raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def baseline_main(path,stat,cancelled,*,directory=None,allow_growth=False):
    """Fingerprints before the WAL cut, bounded memory even for very large DBs."""
    hashes=tempfile.SpooledTemporaryFile(max_size=BASELINE_MEMORY,mode='w+b',dir=directory)
    digest=hashlib.sha256();first=None
    try:
        with path.open('rb',buffering=4*1024**2) as stream:
            wrapper=stream.read(WRAPPER);digest.update(wrapper)
            for number in range((stat.st_size-WRAPPER)//PAGE):
                if cancelled():raise InterruptedError('capture cancelled')
                page=stream.read(PAGE)
                if len(page)!=PAGE:raise AuthenticationError('main database resized during baseline')
                if first is None:first=page
                digest.update(page);hashes.write(hashlib.sha256(b'E'+page).digest())
            if not allow_growth and stream.read(1):raise AuthenticationError('main database grew during baseline')
        current=path.stat()
        if (current.st_dev,current.st_ino)!=(stat.st_dev,stat.st_ino) or not size_matches(current.st_size,stat.st_size,allow_growth):
            raise AuthenticationError('main database replaced or resized during baseline')
        hashes.seek(0)
        return hashes,digest.hexdigest(),wrapper,first
    except BaseException:hashes.close();raise


def reject_hot_journal(source):
    try:
        with Path(str(source)+'-journal').open('rb') as stream:header=stream.read(8)
    except FileNotFoundError:return
    if any(header):raise AuthenticationError('rollback journal requires source-owner recovery; source was not modified')


def size_matches(current,expected,allow_growth):
    return current==expected or (allow_growth and current>expected and (current-WRAPPER)%PAGE==0)


def verify_main(source,identity,wrapper,*,mtime=False,allow_growth=False):
    current=source.stat()
    if (current.st_dev,current.st_ino)!=identity[:2] or not size_matches(current.st_size,identity[2],allow_growth):
        raise AuthenticationError('main database replaced or resized during capture')
    if mtime and current.st_mtime_ns!=identity[3]:
        raise AuthenticationError('main database changed during capture after the two main checks')
    with source.open('rb',buffering=0) as check:
        if check.read(WRAPPER)!=wrapper:
            raise AuthenticationError('main wrapper changed during capture')
    reject_hot_journal(source)
    return current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns


def staged_main_pages(spool):
    spool.seek(0)
    last=0
    while entry:=spool.read(MAIN_RECORD.size):
        if len(entry)!=MAIN_RECORD.size:
            raise AuthenticationError('incomplete owned main snapshot record')
        number,fingerprint=MAIN_RECORD.unpack(entry)
        page=spool.read(PAGE)
        if number<=last or len(page)!=PAGE or hashlib.sha256(b'E'+page).digest()!=fingerprint:
            raise AuthenticationError('owned snapshot bytes changed')
        last=number
        yield number,fingerprint,page


def repaired_main_pages(spool):
    spool.seek(0)
    while record:=spool.read(REPAIR_HEADER.size):
        offset,size,digest=REPAIR_HEADER.unpack(record)
        block=spool.read(size)
        if len(block)!=size or hashlib.sha256(block).digest()!=digest:
            raise AuthenticationError('owned main repair bytes changed')
        for pos in range(0,size,PAGE):
            page=block[pos:pos+PAGE]
            yield (offset+pos)//PAGE+1,hashlib.sha256(b'E'+page).digest(),page


def patched_main_pages(main,repairs):
    if repairs is None:
        yield from main
        return
    # On a collision the guard-certified replacement wins.
    previous=None
    for entry in merge(main,repaired_main_pages(repairs),key=lambda item:item[0]):
        if previous and previous[0]!=entry[0]:yield previous
        previous=entry
    if previous:yield previous


def seal_epoch(source,wal_path,wal_epoch,wal,verified_main,wrapper,shm_identity,cancelled):
    """Finish the optimistic proof, or identify a main-file change to recheck."""
    from .checkpoint import CheckpointGuard
    commit=None
    if wal.committed_frames:
        offset=32+(wal.committed_frames-1)*STRIDE
        wal.stream.seek(offset)
        commit=(offset,wal.stream.read(24))
    if cancelled():raise InterruptedError('capture cancelled')
    guard=CheckpointGuard(source)
    try:
        if guard.identity!=shm_identity:
            raise AuthenticationError('source shared-memory generation changed during capture')
        verify_epoch(wal_path,wal_epoch,commit=commit)
        observed=verify_main(source,verified_main,wrapper,allow_growth=True)
        guard.validate()
        return guard,observed!=verified_main
    finally:
        guard.release()


def guarded_refresh(source,wal_path,wrapper,salt,identity,shm_identity,witness,directory,resources,cancelled):
    """Reconfirm the main using one guarded stream and a bounded sparse reread."""
    from .checkpoint import CheckpointGuard
    repairs=resources.enter_context(tempfile.SpooledTemporaryFile(
        max_size=SNAPSHOT_MEMORY,mode='w+b',dir=directory))
    witness.seek(0)
    guard=CheckpointGuard(source)
    def check():
        guard.validate()
        if cancelled():raise InterruptedError('capture cancelled')
        return False
    try:
        if guard.mode!='win32_sqlite_read0' or guard.identity!=shm_identity:
            raise AuthenticationError('source checkpoint generation changed during capture')
        cut=verify_main(source,identity,wrapper,allow_growth=True)
        source_wal=epoch(wal_path)
        wal_size=wal_path.stat().st_size if source_wal is not None else 0
        if wal_size>MAX_GUARDED_WAL:
            raise AuthenticationError('WAL too large for guarded refresh during capture')
        digest=hashlib.sha256(wrapper)
        added=0
        with source.open('rb',buffering=0) as main:
            if main.read(WRAPPER)!=wrapper:raise AuthenticationError('main wrapper changed during capture')
            for offset in range(0,cut[2]-WRAPPER,MAIN_BLOCK):
                check()
                size=min(MAIN_BLOCK,cut[2]-WRAPPER-offset)
                block=main.read(size)
                if len(block)!=size or (offset==0 and block[:16]!=salt):
                    raise AuthenticationError('main database changed during guarded capture')
                hashed=hashlib.sha256(block).digest()
                digest.update(block)
                if witness.read(32)!=hashed:
                    added+=size
                    if added>MAX_REPAIR_BYTES:
                        raise AuthenticationError('main changes exceed guarded repair limit during capture')
                    repairs.write(REPAIR_HEADER.pack(offset,size,hashed))
                    repairs.write(block)
        wal=freeze(wal_path,directory=directory,cancelled=check,maximum_bytes=MAX_GUARDED_WAL)
        resources.callback(wal.close)
        if source_wal is not None:
            if wal.header!=source_wal[2]:raise AuthenticationError('WAL reset during guarded capture')
            verify_epoch(wal_path,source_wal)
        elif wal.header:
            raise AuthenticationError('WAL appeared during guarded capture')
        verify_live_prefix(wal_path,wal,cancelled=check)
        # Unchanged blocks already matched an independently read SHA-256. The
        # blocks that changed must agree with a second read under protection.
        repairs.seek(0)
        with source.open('rb',buffering=0) as main:
            if main.read(WRAPPER)!=wrapper:raise AuthenticationError('main wrapper changed during capture')
            while record:=repairs.read(REPAIR_HEADER.size):
                check()
                offset,size,expected=REPAIR_HEADER.unpack(record)
                repairs.seek(size,1)
                main.seek(WRAPPER+offset)
                block=main.read(size)
                if len(block)!=size or hashlib.sha256(block).digest()!=expected:
                    raise AuthenticationError('main changed during guarded capture')
        verify_main(source,cut,wrapper,mtime=True)
        verify_live_prefix(wal_path,wal,cancelled=check)
        if source_wal is not None:
            verify_epoch(wal_path,source_wal)
        check()
        return wal,cut,digest.hexdigest(),repairs,added,guard
    finally:
        guard.release()


def update(source: Path, view: Path, password: bytes, *, fault=None, cancelled=lambda:False, deep=True):
    with ExitStack() as resources:
        return _update(source,view,password,resources,fault=fault,cancelled=cancelled,deep=deep)


def _update(source,view,password,resources,*,fault,cancelled,deep):
    started = time.monotonic()
    source = source.resolve(strict=True)
    view = view.resolve()
    view.parent.mkdir(parents=True, exist_ok=True)
    if source == view.resolve() or source.is_relative_to(view.parent.resolve()):
        raise ValueError('source and owned state must be separate')
    from .checkpoint import CheckpointGuard,shared_memory_identity
    wal_path = Path(str(source)+'-wal')
    wal_epoch = epoch(wal_path)
    shm_identity = shared_memory_identity(source) if wal_epoch is not None else None
    reject_hot_journal(source)
    checkpoint=None
    if wal_epoch is None:
        checkpoint=CheckpointGuard(source)
        resources.callback(checkpoint.release)
    last_guard_check=[0.0]
    def input_cancelled():
        if cancelled():return True
        if checkpoint is None:return False
        now=time.monotonic()
        if now-last_guard_check[0]>=.25:
            checkpoint.validate();last_guard_check[0]=now
        return False
    stat=source.stat()
    if (stat.st_size-WRAPPER) <= 0 or (stat.st_size-WRAPPER) % PAGE:
        raise AuthenticationError('unaligned wrapped NTQQ database')
    baseline,before,wrapper,first_page=baseline_main(source,stat,input_cancelled,directory=view.parent,
        allow_growth=wal_epoch is not None)
    resources.callback(baseline.close)
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    salt = first_page[:16]
    cipher = PageCipher(password,salt)
    first_plain=cipher.decrypt(first_page,1)
    if wal_epoch is not None:
        if first_plain[18:20]!=b'\x02\x02':
            raise AuthenticationError('WAL epoch proof requires a WAL-mode main header')
    source_binding = hashlib.sha256(BINDING.encode()+wrapper+salt).hexdigest()
    dirty, pending = view.with_suffix('.dirty'), view.with_suffix('.pending')
    index = sqlite3.connect(view.with_suffix('.pages.sqlite'))
    resources.callback(index.close)
    index.executescript('PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS page(number INTEGER PRIMARY KEY,cipher_hash BLOB NOT NULL,plain_hash BLOB NOT NULL);')
    old_binding = index.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
    if old_binding and old_binding[0] != source_binding:
        index.close()
        raise AuthenticationError('source generation changed; explicit new baseline required')
    reusable = view.exists() and not dirty.exists() and old_binding is not None
    must_deep=deep or not reusable
    # A pending uncommitted row scan always forces full reconciliation.
    changed_numbers=[] if reusable and not pending.exists() else None
    marked = False
    changed = decrypted = 0
    zero_lock_pages=[]
    base_pages = (stat.st_size-WRAPPER)//PAGE
    wal = None
    strategy='wal_epoch_short_guard' if wal_epoch is not None else 'conservative_main_guard'
    repairs=None
    repair_bytes=0
    refresh_guard=None

    def mark():
        nonlocal marked
        if not marked:
            atomic_json(dirty, {'schema':'qq-decoded-dirty/1','binding':source_binding})
            marked = True

    try:
        if wal_epoch is None:
            # Keep the guarded optimistic proof for absent/empty WALs and
            # rollback-mode inputs. It must observe the WAL on both sides.
            wal=freeze(wal_path,directory=view.parent,cancelled=input_cancelled)
            resources.callback(wal.close)
            verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
        snapshot=tempfile.SpooledTemporaryFile(max_size=SNAPSHOT_MEMORY,mode='w+b',dir=view.parent)
        resources.callback(snapshot.close)
        discrepancies=tempfile.SpooledTemporaryFile(max_size=1024**2,mode='w+b',dir=view.parent)
        resources.callback(discrepancies.close)
        witness=resources.enter_context(tempfile.SpooledTemporaryFile(
            max_size=1024**2,mode='w+b',dir=view.parent)) if wal_epoch is not None else None
        main_block=bytearray()
        covered_main_changes=main_snapshot_bytes=0
        base_lock_is_zero=False
        owned_identity=view.stat() if reusable else None
        main_hash = hashlib.sha256()
        known_cursor=index.execute('SELECT number,cipher_hash,plain_hash FROM page ORDER BY number') if reusable else None
        known=known_cursor.fetchone() if known_cursor else None
        with ExitStack() as freezing:
            main=freezing.enter_context(source.open('rb',buffering=4*1024**2))
            previous=freezing.enter_context(view.open('rb',buffering=4*1024**2)) if reusable else None
            current_wrapper=main.read(WRAPPER)
            if current_wrapper!=wrapper:raise AuthenticationError('main wrapper changed during capture')
            main_hash.update(current_wrapper)
            for number in range(1,base_pages+1):
                if input_cancelled(): raise InterruptedError('capture cancelled')
                base = main.read(PAGE)
                if len(base)!=PAGE:raise AuthenticationError('main database resized during capture')
                main_hash.update(base)
                if witness is not None:
                    main_block.extend(base)
                    if len(main_block)==MAIN_BLOCK:
                        witness.write(hashlib.sha256(main_block).digest())
                        main_block.clear()
                fingerprint=hashlib.sha256(b'E'+base).digest()
                if fingerprint!=baseline.read(32):
                    discrepancies.write(PAGE_NUMBER.pack(number))
                    covered_main_changes+=1
                if number==LOCK_PAGE:
                    base_lock_is_zero=not any(base)
                while known and known[0]<number:known=known_cursor.fetchone()
                page_known=known if known and known[0]==number else None
                if page_known:known=known_cursor.fetchone()
                previous_page=previous.read(PAGE) if reusable else None
                if page_known is not None and page_known[1]==fingerprint and hashlib.sha256(previous_page).digest()==page_known[2]:
                    continue
                snapshot.write(MAIN_RECORD.pack(number,fingerprint));snapshot.write(base)
                main_snapshot_bytes+=PAGE+MAIN_RECORD.size
        if main_block:
            witness.write(hashlib.sha256(main_block).digest())
            main_block.clear()
        verified_main=verify_main(source,identity,wrapper,allow_growth=wal_epoch is not None)
        second_sha=main_hash.hexdigest()
        if known_cursor:known_cursor.close()
        if wal_epoch is not None:
            observed_epoch=epoch(wal_path)
            refresh=(observed_epoch is not None and observed_epoch[:2]==wal_epoch[:2]
                     and observed_epoch[2]!=wal_epoch[2])
            if not refresh:
                # Include commits that were backfilled while reading the main.
                verify_epoch(wal_path,wal_epoch)
                wal=freeze(wal_path,directory=view.parent,cancelled=input_cancelled)
                resources.callback(wal.close)
                if wal.header!=wal_epoch[2]:
                    raise AuthenticationError('WAL reset during capture')
                verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
                verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
                checkpoint,refresh=seal_epoch(source,wal_path,wal_epoch,wal,verified_main,wrapper,shm_identity,cancelled)
            if refresh:
                if shm_identity is None:
                    raise AuthenticationError('main or WAL changed during capture without a checkpoint guard')
                if wal is not None:wal.close()
                wal,verified_main,before,repairs,repair_bytes,refresh_guard=guarded_refresh(
                    source,wal_path,wrapper,salt,identity,shm_identity,witness,view.parent,resources,cancelled)
                second_sha=before
                base_pages=(verified_main[2]-WRAPPER)//PAGE
                covered_main_changes=0
                # The merged repair stream supplies the actual page if it
                # replaced the original lock-byte page.
                if base_lock_is_zero:
                    repairs.seek(0)
                    while record:=repairs.read(REPAIR_HEADER.size):
                        repair_offset,repair_size,_=REPAIR_HEADER.unpack(record)
                        if repair_offset<=(LOCK_PAGE-1)*PAGE<repair_offset+repair_size:
                            base_lock_is_zero=False
                            break
                        repairs.seek(repair_size,1)
                strategy='wal_guarded_recheck'
        main_floor=base_pages
        final_pages=wal.database_pages or base_pages
        overlay=WalOverlay(index,wal.committed_frames)
        wal.stream.seek(32)
        largest_commit=base_pages
        for ordinal in range(wal.committed_frames):
            if input_cancelled():raise InterruptedError('capture cancelled')
            number,pages=struct.unpack('>II',wal.stream.read(24)[:8])
            wal.stream.seek(PAGE,1)
            overlay.add(number,32+ordinal*STRIDE+24)
            if pages:
                largest_commit=max(pages,largest_commit)
                main_floor=min(main_floor,pages)
                overlay.shrink(pages)
        observed_main_pages=(verified_main[2]-WRAPPER)//PAGE
        if observed_main_pages>largest_commit:
            raise AuthenticationError('main grew outside frozen committed WAL pages')
        for number in range(base_pages+1,min(observed_main_pages,final_pages)+1):
            if input_cancelled():raise InterruptedError('capture cancelled')
            if number!=LOCK_PAGE and not overlay.contains(number):
                raise AuthenticationError('main grew outside frozen committed WAL pages')
        discrepancies.seek(0)
        if refresh_guard is None:
            while page_number:=discrepancies.read(PAGE_NUMBER.size):
                if input_cancelled():raise InterruptedError('capture cancelled')
                if not overlay.contains(PAGE_NUMBER.unpack(page_number)[0]):
                    raise AuthenticationError('main database changed outside frozen committed WAL pages')
            if wal_epoch is None:
                verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
        if first_plain[18:20]!=b'\x02\x02' and (second_sha!=before or verified_main[3]!=identity[3]):
            raise AuthenticationError('non-WAL main database changed during capture')
        if reusable:
            current_owned=view.stat()
            if (current_owned.st_dev,current_owned.st_ino,current_owned.st_size,current_owned.st_mtime_ns)!=(owned_identity.st_dev,owned_identity.st_ino,owned_identity.st_size,owned_identity.st_mtime_ns):
                raise AuthenticationError('owned view changed during input freezing')
        if refresh_guard is not None:
            checkpoint=refresh_guard
        elif wal_epoch is None:
            checkpoint.validate()
            checkpoint.release()
        input_freeze_seconds=round(time.monotonic()-started,3)
        # Both live prefix proofs and main-page consistency checks are complete.
        # Slow decryption, SQLite writes and structural checks use owned bytes.
        wal.stream.seek(32)
        for ordinal in range(wal.committed_frames):
            if cancelled():raise InterruptedError('capture cancelled')
            frame=wal.stream.read(24);number=struct.unpack('>I',frame[:4])[0]
            cipher.decrypt(wal.stream.read(PAGE),number)
        if not reusable:
            mark();index.execute('DELETE FROM page')
        frozen_pages=snapshot_bytes=0
        main_items=patched_main_pages(staged_main_pages(snapshot),repairs)
        main_entry=next(main_items,None)
        wal_items=iter(overlay.items())
        wal_entry=next(wal_items,None)
        zero=bytes(PAGE)
        zero_hash=hashlib.sha256(b'Z').digest()
        next_zero=main_floor+1
        with view.open('r+b' if reusable else 'w+b') as target:
            while True:
                if cancelled():raise InterruptedError('capture cancelled')
                past_end=final_pages+1
                number=min(main_entry[0] if main_entry else past_end,
                           wal_entry[0] if wal_entry else past_end,next_zero)
                if number>final_pages:break
                base_entry=None
                if main_entry is not None and main_entry[0]==number:
                    base_entry=main_entry
                    main_entry=next(main_items,None)
                if number==next_zero:next_zero+=1
                if wal_entry is not None and wal_entry[0]==number:
                    wal.stream.seek(wal_entry[1])
                    encrypted=wal.stream.read(PAGE)
                    fingerprint=hashlib.sha256(b'E'+encrypted).digest()
                    wal_entry=next(wal_items,None)
                elif number>main_floor:
                    encrypted=None
                    fingerprint=zero_hash
                else:
                    _,fingerprint,encrypted=base_entry
                lock_zero=number==LOCK_PAGE and encrypted==zero
                if lock_zero:zero_lock_pages.append(number)
                offset=(number-1)*PAGE
                page_known=index.execute('SELECT cipher_hash,plain_hash FROM page WHERE number=?',(number,)).fetchone() if reusable else None
                if page_known is not None and page_known[0]==fingerprint:
                    target.seek(offset)
                    if hashlib.sha256(target.read(PAGE)).digest()==page_known[1]:
                        continue
                if changed_numbers is not None and (page_known is None or page_known[0]!=fingerprint):
                    if len(changed_numbers)==MAX_DELTA_PAGES:
                        changed_numbers=None  # Full reconciliation for a large delta.
                    else:
                        changed_numbers.append(number)
                frozen_pages+=1
                if encrypted is None or lock_zero:
                    plain=zero
                else:
                    plain=cipher.decrypt(encrypted,number)
                    decrypted+=1
                    snapshot_bytes+=PAGE
                digest=hashlib.sha256(plain).digest()
                mark();target.seek(offset)
                if not reusable or hashlib.sha256(target.read(PAGE)).digest()!=digest:
                    target.seek(offset);target.write(plain);changed+=1
                index.execute('INSERT OR REPLACE INTO page VALUES(?,?,?)',(number,fingerprint,digest))
                if fault:fault('after_page')
            if target.seek(0,2)!=final_pages*PAGE:
                mark();target.truncate(final_pages*PAGE)
            if marked:
                target.flush();os.fsync(target.fileno())
        if base_lock_is_zero and LOCK_PAGE<=main_floor and not overlay.contains(LOCK_PAGE) and not zero_lock_pages:
            zero_lock_pages.append(LOCK_PAGE)
        touched_tables=None;row_windows=None;root_map=None
        if marked or changed_numbers is not None:
            # dbstat maps changed final pages (including overflow pages) back to
            # current tables. It does not use timestamps or message-ID windows.
            # Removed rows remain observations; unobserved deletion is not a new row.
            if changed_numbers is not None:
                try:
                    from .native_sqlite import runtime_directory,readonly
                    if (runtime_directory()/'runtime.json').is_file():
                        check=readonly(view,cancelled=cancelled)
                    else:
                        raise ImportError('private reader not built')
                except ImportError:
                    try:
                        import apsw
                        check=apsw.Connection(view.as_uri()+'?mode=ro&immutable=1',flags=apsw.SQLITE_OPEN_READONLY|apsw.SQLITE_OPEN_URI)
                    except ImportError:
                        check=sqlite3.connect(view.as_uri()+'?mode=ro&immutable=1',uri=True)
                try:
                    from .row_locator import locate
                    previous=index.execute("SELECT value FROM metadata WHERE key='root_map'").fetchone()
                    row_windows,root_map=locate(check,view,changed_numbers,json.loads(previous[0]) if previous else None)
                    touched_tables=list(row_windows) if row_windows is not None else None
                except Exception:
                    # Builds without dbstat retain the full-table algorithm.
                    touched_tables=None
                finally:check.close()
        if must_deep:
            from .native_sqlite import decoded_connection
            check=decoded_connection(view,cancelled=cancelled)
            try:
                integrity=check.execute('PRAGMA quick_check').fetchone()[0]
                if integrity!='ok':raise AuthenticationError('decoded SQLite structural check failed: '+str(integrity))
            except BaseException:
                atomic_json(dirty,{'schema':'qq-decoded-dirty/1','binding':source_binding})
                raise
            finally:check.close()
        if marked:
            index.execute('DELETE FROM page WHERE number>?',(final_pages,))
            index.execute("INSERT OR REPLACE INTO metadata VALUES('binding',?)",(source_binding,))
            if root_map is None:
                from .native_sqlite import decoded_connection
                from .row_locator import roots
                check=decoded_connection(view)
                try:root_map=roots(check)
                finally:check.close()
            index.execute("INSERT OR REPLACE INTO metadata VALUES('root_map',?)",(json.dumps(root_map,sort_keys=True),))
            index.commit()
            atomic_json(pending,{'schema':'qq-decoded-pending/1','binding':source_binding,'touched_tables':touched_tables,'row_windows':row_windows})
            dirty.unlink()
        if fault: fault('before_catalog')
        return {'binding':source_binding,'changed_pages':changed,'decrypted_pages':decrypted,
                'database_pages':final_pages,'wal_committed_frames':wal.committed_frames,
                'wal_prefix_sha256':wal.prefix_hash,'main_sha256':before,
                'main_second_sha256':second_sha,'main_changes_covered_by_wal':covered_main_changes,
                'needs_scan':pending.exists(),'touched_tables':touched_tables,'zero_lock_pages':zero_lock_pages,
                'input_freeze_seconds':input_freeze_seconds,'frozen_pages':frozen_pages,'snapshot_bytes':snapshot_bytes,
                'source_read_guard':checkpoint.mode,'checkpoint_guard_seconds':checkpoint.seconds,
                'input_cut_strategy':strategy,
                'baseline_proof_bytes':(identity[2]-WRAPPER)//PAGE*32,'wal_prefix_bytes':wal.prefix_bytes,
                'staged_main_bytes':main_snapshot_bytes,
                'repair_bytes':repair_bytes,
                'wal_overlay_spilled':overlay.connection is not None,
                'row_windows':row_windows,
                'validation':'sqlite_quick_check' if must_deep else 'authenticated_delta',
                'seconds':round(time.monotonic()-started,3)}
    except BaseException:
        index.rollback()
        raise

"""Stable-main/two-prefix capture with one rebuildable decoded view.

Every accepted main image is read twice around WAL freezing. Only changed
final pages are decrypted/written; committed WAL frames are authenticated.
Only necessary encrypted pages enter a bounded owned spool. Decryption and
SQLite writes happen after the live cut, so source activity cannot extend that
window. Dirty/pending markers protect view writes and catalog acceptance.
"""
from __future__ import annotations

import hashlib
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
from .wal import freeze, verify_live_prefix, STRIDE


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


def baseline_main(path,stat,cancelled):
    """Fingerprints before the WAL cut, bounded memory even for very large DBs."""
    hashes=tempfile.SpooledTemporaryFile(max_size=64*1024**2,mode='w+b')
    digest=hashlib.sha256();first=None
    try:
        with path.open('rb',buffering=4*1024**2) as stream:
            wrapper=stream.read(WRAPPER);digest.update(wrapper)
            for number in range((stat.st_size-WRAPPER)//PAGE):
                if cancelled():raise InterruptedError('capture cancelled')
                page=stream.read(PAGE)
                if len(page)!=PAGE:raise AuthenticationError('main database resized during baseline')
                if first is None:first=page
                digest.update(page);hashes.write(hashlib.sha256(page).digest())
            if stream.read(1):raise AuthenticationError('main database grew during baseline')
        current=path.stat()
        if (current.st_dev,current.st_ino,current.st_size)!=(stat.st_dev,stat.st_ino,stat.st_size):raise AuthenticationError('main database replaced or resized during baseline')
        hashes.seek(0)
        return hashes,digest.hexdigest(),wrapper,first
    except BaseException:hashes.close();raise


def reject_hot_journal(source):
    try:
        with Path(str(source)+'-journal').open('rb') as stream:header=stream.read(8)
    except FileNotFoundError:return
    if any(header):raise AuthenticationError('rollback journal requires source-owner recovery; source was not modified')


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
    stat = source.stat()
    reject_hot_journal(source)
    if (stat.st_size-WRAPPER) <= 0 or (stat.st_size-WRAPPER) % PAGE:
        raise AuthenticationError('unaligned wrapped NTQQ database')
    from .checkpoint import CheckpointGuard
    checkpoint=CheckpointGuard(source);resources.callback(checkpoint.release)
    last_guard_check=[0.0]
    def input_cancelled():
        if cancelled():return True
        now=time.monotonic()
        if now-last_guard_check[0]>=.25:
            checkpoint.validate();last_guard_check[0]=now
        return False
    stat=source.stat()  # Size and file identity are sampled under the read guard.
    baseline,before,wrapper,first_page=baseline_main(source,stat,input_cancelled)
    resources.callback(baseline.close)
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    salt = first_page[:16]
    cipher = PageCipher(password,salt)
    first_plain=cipher.decrypt(first_page,1)
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
    wal_path = Path(str(source)+'-wal')
    wal = freeze(wal_path,cancelled=input_cancelled)
    resources.callback(wal.close)
    marked = False
    changed = decrypted = 0
    covered_main_changes=0
    zero_lock_pages=[]
    base_pages = (stat.st_size-WRAPPER)//PAGE
    main_floor = base_pages
    final_pages = wal.database_pages or base_pages
    overlay = {}

    def mark():
        nonlocal marked
        if not marked:
            atomic_json(dirty, {'schema':'qq-decoded-dirty/1','binding':source_binding})
            marked = True

    try:
        # Build the frozen overlay first. Cryptographic frame authentication is
        # deliberately after input freezing, outside the live consistency window.
        wal.stream.seek(32)
        for ordinal in range(wal.committed_frames):
            if input_cancelled(): raise InterruptedError('capture cancelled')
            frame = wal.stream.read(24)
            number,pages = struct.unpack('>II',frame[:8])
            wal.stream.seek(PAGE,1)
            overlay[number] = 32+ordinal*STRIDE+24
            if pages:
                main_floor = min(main_floor,pages)
                overlay = {n:offset for n,offset in overlay.items() if n <= pages}
        verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
        snapshot=tempfile.SpooledTemporaryFile(max_size=64*1024**2,mode='w+b',dir=view.parent)
        resources.callback(snapshot.close)
        record=struct.Struct('<Q32sB')
        frozen_pages=snapshot_bytes=0
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
            # Main pages beyond a WAL shrink still participate in the stable hash.
            for number in range(1,max(base_pages,final_pages)+1):
                if input_cancelled(): raise InterruptedError('capture cancelled')
                base = main.read(PAGE) if number <= base_pages else b''
                main_hash.update(base)
                if number<=base_pages:
                    expected=baseline.read(32)
                    if len(base)!=PAGE:raise AuthenticationError('main database resized during capture')
                    if hashlib.sha256(base).digest()!=expected:
                        if number not in overlay:raise AuthenticationError('main database changed outside frozen committed WAL pages')
                        covered_main_changes+=1
                if number > final_pages: continue
                if number in overlay:
                    wal.stream.seek(overlay[number]); encrypted=wal.stream.read(PAGE)
                elif number <= main_floor:
                    encrypted=base
                else:
                    encrypted=None
                fingerprint=hashlib.sha256(b'E'+encrypted if encrypted is not None else b'Z').digest()
                lock_zero=(encrypted is not None and number==(0x40000000-WRAPPER)//PAGE+1 and not any(encrypted))
                if lock_zero:zero_lock_pages.append(number)
                while known and known[0]<number:known=known_cursor.fetchone()
                page_known=known if known and known[0]==number else None
                if page_known:known=known_cursor.fetchone()
                if changed_numbers is not None and (page_known is None or page_known[1]!=fingerprint):changed_numbers.append(number)
                current_hash=None
                if reusable:
                    current_hash=hashlib.sha256(previous.read(PAGE)).digest()
                if reusable and page_known is not None and page_known[1]==fingerprint:
                    if current_hash==page_known[2]:
                        continue
                flags=2 if encrypted is None else 1 if lock_zero else 0
                snapshot.write(record.pack(number,fingerprint,flags));frozen_pages+=1
                if flags==0:snapshot.write(encrypted);snapshot_bytes+=PAGE
            current=source.stat()
            if covered_main_changes and first_plain[18:20]!=b'\x02\x02':
                raise AuthenticationError('checkpoint tolerance requires a WAL-mode main header')
            if first_plain[18:20]!=b'\x02\x02' and (main_hash.hexdigest()!=before or current.st_mtime_ns!=identity[3]):
                raise AuthenticationError('non-WAL main database changed during capture')
            if (current.st_dev,current.st_ino,current.st_size)!=identity[:3]:
                raise AuthenticationError('main database replaced or resized during capture')
            with source.open('rb') as wrapper_check:
                if wrapper_check.read(WRAPPER)!=wrapper:raise AuthenticationError('main wrapper changed during capture')
            reject_hot_journal(source)
            verify_live_prefix(wal_path,wal,cancelled=input_cancelled)
        if known_cursor:known_cursor.close()
        if reusable:
            current_owned=view.stat()
            if (current_owned.st_dev,current_owned.st_ino,current_owned.st_size,current_owned.st_mtime_ns)!=(owned_identity.st_dev,owned_identity.st_ino,owned_identity.st_size,owned_identity.st_mtime_ns):
                raise AuthenticationError('owned view changed during input freezing')
        checkpoint.validate();checkpoint.release()
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
        snapshot.seek(0)
        last_frozen_number=0
        with view.open('r+b' if reusable else 'w+b') as target:
            for _ in range(frozen_pages):
                if cancelled():raise InterruptedError('capture cancelled')
                number,fingerprint,flags=record.unpack(snapshot.read(record.size))
                if not last_frozen_number<number<=final_pages:raise AuthenticationError('invalid owned snapshot page order')
                last_frozen_number=number
                if flags==0:
                    encrypted=snapshot.read(PAGE)
                    if hashlib.sha256(b'E'+encrypted).digest()!=fingerprint:raise AuthenticationError('owned snapshot bytes changed')
                    plain=cipher.decrypt(encrypted,number)
                elif flags==1 and number==(0x40000000-WRAPPER)//PAGE+1 and fingerprint==hashlib.sha256(b'E'+bytes(PAGE)).digest():plain=bytes(PAGE)
                elif flags==2 and number>main_floor and number not in overlay and fingerprint==hashlib.sha256(b'Z').digest():plain=bytes(PAGE)
                else:raise AuthenticationError('invalid owned snapshot page flags')
                decrypted+=flags==0;digest=hashlib.sha256(plain).digest()
                mark();target.seek((number-1)*PAGE)
                if not reusable or hashlib.sha256(target.read(PAGE)).digest()!=digest:
                    target.seek((number-1)*PAGE);target.write(plain);changed+=1
                index.execute('INSERT OR REPLACE INTO page VALUES(?,?,?)',(number,fingerprint,digest))
                if fault:fault('after_page')
            if target.seek(0,2)!=final_pages*PAGE:
                mark();target.truncate(final_pages*PAGE)
            if marked:
                target.flush();os.fsync(target.fileno())
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
                'main_second_sha256':main_hash.hexdigest(),'main_changes_covered_by_wal':covered_main_changes,
                'needs_scan':pending.exists(),'touched_tables':touched_tables,'zero_lock_pages':zero_lock_pages,
                'input_freeze_seconds':input_freeze_seconds,'frozen_pages':frozen_pages,'snapshot_bytes':snapshot_bytes,
                'source_read_guard':checkpoint.mode,'checkpoint_guard_seconds':checkpoint.seconds,
                'row_windows':row_windows,
                'validation':'sqlite_quick_check' if must_deep else 'authenticated_delta',
                'seconds':round(time.monotonic()-started,3)}
    except BaseException:
        index.rollback()
        raise

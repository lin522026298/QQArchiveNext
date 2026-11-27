"""Rolling point retention and identity-preserving, out-of-place compaction.

No shared block is deleted in place. A checked replacement keeps block IDs,
point descriptor bytes, repository identity and pinned relationships intact.
The sibling lease and durable swap journal survive directory replacement.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import threading
import time
import uuid
import zlib

from .materialize import atomic_json

DEFAULT_RECENT=2
DEFAULT_DAYS=7
_held=threading.local()


def _safe(path):
    path=Path(path).absolute()
    for item in (path,*path.parents):
        if item.exists():
            s=item.lstat()
            if item.is_symlink() or getattr(s,'st_file_attributes',0)&0x400:
                raise ValueError('repository path contains a reparse point')
    return path.resolve()


@contextmanager
def lease(repo,*,write=False,cancelled=lambda:False):
    """Shared reads, exclusive mutations; the lock lives outside the generation."""
    repo=_safe(repo);key=str(repo).casefold();held=getattr(_held,'values',{})
    if key in held:
        if write and not held[key]:raise RuntimeError('cannot upgrade a repository read lease')
        yield repo;return
    repo.parent.mkdir(parents=True,exist_ok=True)
    path=repo.parent/('.'+repo.name+'.lease')
    _safe(path)
    with path.open('a+b') as stream:
        if not path.stat().st_size:stream.write(b'0');stream.flush()
        if os.name=='nt':
            from ctypes import wintypes as W
            class OVERLAPPED(ctypes.Structure):
                _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                          ('Offset',W.DWORD),('OffsetHigh',W.DWORD),('hEvent',W.HANDLE)]
            import msvcrt
            handle=W.HANDLE(msvcrt.get_osfhandle(stream.fileno()));overlap=OVERLAPPED()
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.LockFileEx.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,W.DWORD,ctypes.POINTER(OVERLAPPED)]
            kernel.LockFileEx.restype=W.BOOL
            kernel.UnlockFileEx.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,ctypes.POINTER(OVERLAPPED)]
            while not kernel.LockFileEx(handle,1|(2 if write else 0),0,1,0,ctypes.byref(overlap)):
                error=ctypes.get_last_error()
                if error not in (32,33,158):raise ctypes.WinError(error)
                if cancelled():raise InterruptedError('repository lease cancelled')
                time.sleep(.1)
        else:
            import fcntl
            while True:
                try:fcntl.flock(stream.fileno(),(fcntl.LOCK_EX if write else fcntl.LOCK_SH)|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if cancelled():raise InterruptedError('repository lease cancelled')
                    time.sleep(.1)
        held[key]=write;_held.values=held
        try:
            if (repo.parent/('.'+repo.name+'.swap.json')).exists() and not getattr(_held,'recovering',False):
                raise ValueError('interrupted repository switch; run repository.recover before use')
            yield repo
        finally:
            del held[key]
            if os.name=='nt':kernel.UnlockFileEx(handle,0,1,0,ctypes.byref(overlap))
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def guarded(*,write=False):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(repo,*args,**kwargs):
            with lease(repo,write=write,cancelled=kwargs.get('cancelled',lambda:False)):
                return function(repo,*args,**kwargs)
        return wrapped
    return decorate


def policy(repo):
    path=Path(repo)/'RETENTION.json'
    value=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
      'schema':'qq-next-retention/1','recent':DEFAULT_RECENT,'compact_days':DEFAULT_DAYS,
      'last_compaction':time.time(),'retired_total':0,'compacted_retired_total':0}
    if value.get('schema')!='qq-next-retention/1' or value.get('recent')!=2 or value.get('compact_days')!=7:
        raise ValueError('invalid rolling repository policy')
    return value


def selection(repo):
    from .backup import repository,read_point
    repo=repository(repo)
    refs=json.loads((repo/'refs.json').read_text(encoding='utf-8'))
    if refs.get('schema')!='qq-next-backup-refs/1' or not refs.get('recent') or not isinstance(refs.get('pinned'),list):
        raise ValueError('invalid backup refs')
    keep=list(dict.fromkeys(refs['recent'][:2]+refs['pinned']))
    points=[read_point(repo,item)[1] for item in keep]
    # The order in recent is the successful publication order, not filesystem time.
    return refs,points


@guarded()
def status(repo,*,cancelled=lambda:False):
    from .backup import repository
    repo=repository(repo)
    if not (repo/'refs.json').exists():
        return {'policy':policy(repo),'keep':[],'pinned':[],'recent':[],'rolling_enabled':(repo/'RETENTION.json').exists(),
          'points':len(list((repo/'points').glob('*.json'))),'expired':0,'cleanup':None,'state':'no_committed_point',
          'block_pack_bytes':(repo/'blocks.sqlite').stat().st_size if (repo/'blocks.sqlite').exists() else 0}
    refs,points=selection(repo);value=policy(repo)
    existing=sorted(p.stem for p in (Path(repo)/'points').glob('*.json'))
    keep={p['id'] for p in points}
    return {'policy':value,'keep':[p['id'] for p in points],'pinned':refs['pinned'],
      'rolling_enabled':(Path(repo)/'RETENTION.json').exists(),
      'cleanup':json.loads((Path(repo)/'CLEANUP_RECEIPT.json').read_text(encoding='utf-8')) if (Path(repo)/'CLEANUP_RECEIPT.json').exists() else None,
      'recent':refs['recent'][:2],'points':len(existing),'expired':len(set(existing)-keep),
      'block_pack_bytes':(Path(repo)/'blocks.sqlite').stat().st_size if (Path(repo)/'blocks.sqlite').exists() else 0,
      'physical_reclaim':'requires a verified replacement; payload and filesystem allocation differ'}


def retain(repo,*,cancelled=lambda:False):
    """Called only after a successful point commit and closed block writer.

    Publishing a failed/cancelled backup never reaches this function. Payloads
    remain until a verified replacement, including after a partial retirement.
    """
    from .backup import read_point
    repo=Path(repo);refs,points=selection(repo);value=policy(repo)
    keep={p['id'] for p in points};retired=[]
    for path in sorted((repo/'points').glob('*.json')):
        if cancelled():raise InterruptedError('retention cancelled')
        if path.stem in keep:continue
        _,point=read_point(repo,path.stem)
        retired.append({'id':point['id'],'descriptor_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'created':point['created']})
    # Validate the entire retirement set before touching any point.
    compact_refs=dict(refs,recent=refs['recent'][:2])
    atomic_json(repo/'refs.json',compact_refs)
    prior=repo/'RETIREMENT_RECEIPT.json'
    old=json.loads(prior.read_text(encoding='utf-8')) if prior.exists() else {}
    receipt={'schema':'qq-next-retirement/1','time':time.time(),'keep':list(keep),'pinned':refs['pinned'],
             'retired_total':value['retired_total']+len(retired),
             'descriptors':(old.get('descriptors',[])+retired)[-200:]}
    atomic_json(prior,receipt)
    for item in retired:
        path=repo/'points'/(item['id']+'.json')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item['descriptor_sha256']:
            raise ValueError('point changed before retirement')
        path.unlink()
    value['retired_total']=receipt['retired_total'];atomic_json(repo/'RETENTION.json',value)
    return {'retained':len(keep),'retired':len(retired),'recent':compact_refs['recent'],'pinned':refs['pinned']}


def after_backup(repo,result,*,cancelled=lambda:False):
    # A maintenance error does not turn a committed backup into a failed point.
    try:
        if not (Path(repo)/'RETENTION.json').exists():
            result['retention']={'state':'upgrade_required','message':'existing repository preserved; run repository.compact to adopt rolling retention'}
            return result
        result['retention']=retain(repo,cancelled=cancelled)
        value=policy(repo)
        if value['retired_total']>value['compacted_retired_total'] and time.time()-value['last_compaction']>=value['compact_days']*86400:
            result['compaction']=compact(repo,cancelled=cancelled)
    except Exception as exc:
        result['repository_maintenance']={'state':'cancelled' if isinstance(exc,InterruptedError) else 'error','error':str(exc)}
    return result


def due(repo):
    if not (Path(repo)/'RETENTION.json').exists():return False
    value=policy(repo)
    return value['retired_total']>value['compacted_retired_total'] and time.time()-value['last_compaction']>=value['compact_days']*86400


def _fingerprint(path):
    s=path.stat();return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]


def _walk_files(root):
    root=_safe(root)
    for folder,dirs,names in os.walk(root,followlinks=False):
        for name in dirs+names:_safe(Path(folder)/name)
        for name in sorted(names):yield Path(folder)/name


@guarded()
def rebuild(repo,destination,*,cancelled=lambda:False,progress=lambda phase,count:None):
    """Build an independent pack; immutable blobs may share same-volume bytes.

    Every reachable block and blob is decoded/hashed. IDs and descriptor bytes
    are copied exactly; codec-2 legacy slices become self-contained blocks.
    Graph bookkeeping is disk backed, bounded by batches rather than account size.
    """
    from .backup import repository,read_point,point_files,read_chunk
    from .backup_blocks import Pack,DATA,LIST,FILES,INDEX,MAGIC,REFERENCE
    from .backup_unified import blob_path
    from .config import _inside_resolved
    from .history import sha_file
    repo=repository(repo);destination=_safe(destination)
    if destination.exists() or destination.is_relative_to(repo) or repo.is_relative_to(destination):
        raise ValueError('compaction requires a new separate destination')
    refs,points=selection(repo);keep={p['id'] for p in points}
    source_refs=(repo/'refs.json').read_bytes();source_pack=_fingerprint(repo/'blocks.sqlite') if (repo/'blocks.sqlite').exists() else None
    destination.mkdir(parents=True)
    atomic_json(destination/'INCOMPLETE.json',{'schema':'qq-next-compaction-building/1'})
    shutil.copyfile(repo/'OWNER.json',destination/'OWNER.json')
    graph=sqlite3.connect((destination/'reachability.sqlite').as_uri(),uri=True)
    graph.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-16384;'
      'CREATE TABLE wanted(id INTEGER PRIMARY KEY,kind INTEGER NOT NULL,bytes INTEGER NOT NULL,span INTEGER NOT NULL,digest TEXT NOT NULL,expanded INTEGER NOT NULL DEFAULT 0);'
      'CREATE INDEX pending ON wanted(id) WHERE kind!=0 AND expanded=0;'
      'CREATE TABLE blob(digest TEXT PRIMARY KEY,bytes INTEGER NOT NULL) WITHOUT ROWID;'
      'CREATE TABLE chunk(digest TEXT PRIMARY KEY,bytes INTEGER NOT NULL) WITHOUT ROWID;')
    source=Pack(repo,readonly=True,cancelled=cancelled) if source_pack else None
    copied=Pack(destination,cancelled=cancelled,bulk_build=True) if source_pack else None
    upsert='''INSERT INTO wanted(id,kind,bytes,span,digest) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET digest=NULL
      WHERE wanted.kind!=excluded.kind OR wanted.bytes!=excluded.bytes OR wanted.span!=excluded.span OR wanted.digest!=excluded.digest'''
    def want(r):
        if r['kind'] not in (DATA,LIST,FILES,INDEX) or not isinstance(r['id'],int) or r['id']<=0:raise ValueError('invalid block reference')
        graph.execute(upsert,(r['id'],r['kind'],r['bytes'],r['span'],r['sha256']))
    def blob(digest,size,table):
        from .backup import object_path
        object_path(repo,digest)
        graph.execute(f'INSERT INTO {table} VALUES(?,?) ON CONFLICT(digest) DO UPDATE SET bytes=NULL WHERE {table}.bytes!=excluded.bytes',(digest,size))
    started=time.time();files=logical=0;virtual_root=(destination/'_paths').resolve()
    try:
        for point in points:
            if point['body']['schema']!='qq-next-backup-point/1':want(point['body']['files']['root'])
            for name,entry in point_files(repo,point,cancelled=cancelled):
                if cancelled():raise InterruptedError('repository rebuild cancelled')
                _inside_resolved(virtual_root,name)
                if not isinstance(entry['bytes'],int) or entry['bytes']<0:raise ValueError('invalid file length')
                from .backup import object_path
                object_path(repo,entry['sha256'])
                if 'blob' in entry:
                    if entry['blob']!=entry['sha256']:raise ValueError('blob identity differs')
                    blob(entry['blob'],entry['bytes'],'blob')
                elif 'root' in entry:want(entry['root'])
                else:
                    for chunk in entry['chunks']:blob(chunk['sha256'],chunk['bytes'],'chunk')
                files+=1;logical+=entry['bytes']
                if files%1024==0:progress('file_maps',files)
            progress('file_maps',files)
        # Authenticate and expand each metadata block once, keeping DATA on disk.
        count=0
        while True:
            rows=graph.execute('SELECT id,kind,bytes,span,digest FROM wanted WHERE kind!=0 AND expanded=0 LIMIT 1024').fetchall()
            if not rows:break
            for identity,kind,size,span,digest in rows:
                r={'id':identity,'kind':kind,'bytes':size,'span':span,'sha256':digest};payload=source.get(r)
                if kind==LIST:
                    if not payload.startswith(MAGIC) or (len(payload)-len(MAGIC))%REFERENCE.size:raise ValueError('invalid backup list')
                    children=[{'id':a,'kind':b,'bytes':c,'span':d,'sha256':e.hex()} for a,b,c,d,e in REFERENCE.iter_unpack(payload[len(MAGIC):])]
                    if not 1<=len(children)<=64 or sum(c['span'] for c in children)!=span or any(c['kind'] not in (DATA,LIST) for c in children):raise ValueError('invalid backup list span')
                    for child in children:want(child)
                elif kind==INDEX:
                    for child in json.loads(payload).values():want(child)
                elif kind==FILES:
                    for entry in json.loads(payload).values():
                        if 'root' in entry:want(entry['root'])
                graph.execute('UPDATE wanted SET expanded=1 WHERE id=?',(identity,));count+=1
            graph.commit();progress('metadata_blocks',count)
        # Join on integer IDs in ascending order, never build a Python set of pages.
        graph.execute('ATTACH DATABASE ? AS source',(source.path.resolve().as_uri()+'?mode=ro',)) if source else None
        block_count=payload_bytes=0
        if source:
            sql='''SELECT s.*,w.kind AS expected_kind,w.bytes AS expected_bytes,w.span AS expected_span,w.digest AS expected_digest
              FROM wanted w LEFT JOIN source.block s ON s.id=w.id ORDER BY w.id'''
            graph.row_factory=sqlite3.Row
            for row in graph.execute(sql):
                if cancelled():raise InterruptedError('repository rebuild cancelled')
                if row['id'] is None or row['kind']!=row['expected_kind'] or row['byte_count']!=row['expected_bytes'] or bytes(row['digest']).hex()!=row['expected_digest']:
                    raise ValueError('missing or mismatched reachable block')
                data=source.decode(row)
                if row['kind']==DATA and row['expected_span']!=len(data):raise ValueError('data span differs')
                codec=row['codec'];encoded=bytes(row['data'])
                if codec==2:
                    compressed=zlib.compress(data,3);encoded,codec=(compressed,1) if len(compressed)+16<len(data) else (data,0)
                copied.db.execute('INSERT INTO block(id,digest,kind,byte_count,codec,data) VALUES(?,?,?,?,?,?)',
                  (row['id'],row['digest'],row['kind'],row['byte_count'],codec,encoded))
                block_count+=1;payload_bytes+=len(encoded)
                if block_count%8192==0:copied.commit();progress('payload_blocks',block_count)
            copied.commit()
            progress('pack_index',block_count)
            if cancelled():raise InterruptedError('repository index build cancelled')
            copied.db.set_progress_handler(lambda:int(cancelled()),100000)
            try:
                copied.db.execute('CREATE UNIQUE INDEX blocks_by_digest ON block(digest)');copied.commit()
            except sqlite3.OperationalError:
                if cancelled():raise InterruptedError('repository index build cancelled') from None
                raise
            finally:copied.db.set_progress_handler(None,0)
            progress('pack_integrity',block_count)
            if cancelled():raise InterruptedError('repository integrity check cancelled')
            copied.db.set_progress_handler(lambda:int(cancelled()),100000)
            try:
                if copied.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('new pack integrity failure')
            except sqlite3.OperationalError:
                if cancelled():raise InterruptedError('repository integrity check cancelled') from None
                raise
            finally:copied.db.set_progress_handler(None,0)
            if cancelled():raise InterruptedError('repository integrity check cancelled')
        progress('blobs',0)
        blob_count=blob_bytes=linked=0
        for digest,size in graph.execute('SELECT digest,bytes FROM blob ORDER BY digest'):
            if cancelled():raise InterruptedError('repository rebuild cancelled')
            origin=blob_path(repo,digest);before=_fingerprint(origin)
            if sha_file(origin,cancelled)!=(size,digest):raise ValueError('reachable blob hash failure')
            target=blob_path(destination,digest);target.parent.mkdir(parents=True,exist_ok=True)
            try:os.link(origin,target);linked+=size
            except OSError:
                shutil.copyfile(origin,target)
                if sha_file(target,cancelled)!=(size,digest):raise ValueError('copied blob hash failure')
                with target.open('r+b') as stream:os.fsync(stream.fileno())
            if before!=_fingerprint(origin):raise ValueError('source blob changed during rebuild')
            blob_count+=1;blob_bytes+=size
            if blob_count%512==0:progress('blobs',blob_count)
        chunk_count=0
        for digest,size in graph.execute('SELECT digest,bytes FROM chunk ORDER BY digest'):
            from .backup import object_path
            read_chunk(repo,{'sha256':digest,'bytes':size})
            target=object_path(destination,digest);target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(object_path(repo,digest),target);read_chunk(destination,{'sha256':digest,'bytes':size});chunk_count+=1
        if (repo/'refs.json').read_bytes()!=source_refs or (source_pack and _fingerprint(repo/'blocks.sqlite')!=source_pack):
            raise ValueError('source generation changed; rebuild a fresh stable selection')
        (destination/'points').mkdir()
        descriptor_hashes={}
        for point in points:
            name=point['id']+'.json';raw=(repo/'points'/name).read_bytes()
            read_point(repo,point['id'])
            with (destination/'points'/name).open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            descriptor_hashes[point['id']]=hashlib.sha256(raw).hexdigest()
        atomic_json(destination/'refs.json',dict(refs,recent=refs['recent'][:2]))
        retired=[]
        for path in sorted((repo/'points').glob('*.json')):
            if path.stem not in keep:
                _,expired=read_point(repo,path.stem)
                retired.append({'id':expired['id'],'created':expired['created'],'descriptor_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        value=policy(repo);value['retired_total']+=len(retired)
        value['last_compaction']=time.time();value['compacted_retired_total']=value['retired_total']
        atomic_json(destination/'RETENTION.json',value)
        for name in ('RETIREMENT_RECEIPT.json',):
            if (repo/name).exists():shutil.copyfile(repo/name,destination/name)
        if retired:
            atomic_json(destination/'RETIREMENT_RECEIPT.json',{'schema':'qq-next-retirement/1','time':time.time(),
              'keep':list(keep),'pinned':refs['pinned'],'retired_total':value['retired_total'],'descriptors':retired[-200:]})
        receipt={'schema':'qq-next-compaction/1','time':time.time(),'seconds':time.time()-started,
          'keep':[p['id'] for p in points],'pinned':refs['pinned'],'descriptor_sha256':descriptor_hashes,
          'source_refs_sha256':hashlib.sha256(source_refs).hexdigest(),'source_pack':source_pack,
          'blocks_verified':block_count,'encoded_block_bytes':payload_bytes,'unique_blobs_hashed':blob_count,
          'unique_blob_bytes':blob_bytes,'hardlinked_bytes':linked,'legacy_chunks_verified':chunk_count,
          'point_file_observations':files,'point_logical_bytes_sum':logical,
          'expired_descriptors':retired,
          'boundary':'all dependency payloads hashed; full restore/semantic acceptance is separate'}
        atomic_json(destination/'COMPACTION_RECEIPT.json',receipt)
    finally:
        graph.close()
        if source:source.close()
        if copied:
            copied.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');copied.db.execute('PRAGMA journal_mode=DELETE');copied.close()
    for name in ('blocks.sqlite','OWNER.json'):
        if (destination/name).is_file():
            with (destination/name).open('r+b') as stream:os.fsync(stream.fileno())
    (destination/'reachability.sqlite').unlink()
    (destination/'INCOMPLETE.json').unlink()
    progress('complete',files)
    return receipt


def _journal(repo):return repo.parent/('.'+repo.name+'.swap.json')


def _dedicated(repo):
    """Only a dedicated payload warehouse can be replaced, never a portable app."""
    repo=_safe(repo)
    files={'OWNER.json','refs.json','blocks.sqlite','blocks.sqlite-wal','blocks.sqlite-shm',
           'RETENTION.json','RETIREMENT_RECEIPT.json','COMPACTION_RECEIPT.json','CLEANUP_RECEIPT.json'}
    for path in repo.iterdir():
        _safe(path)
        if path.is_file() and path.name in files:continue
        if path.is_dir() and path.name in ('points','blobs','chunks'):continue
        if path.is_dir() and path.name.startswith('staging-') and len(path.name)==40 and all(c in '0123456789abcdef' for c in path.name[8:]):continue
        raise ValueError('invalid repository maintenance target: use a dedicated warehouse; portable exports, restored states and unknown files are preserved')


def _checked_journal(repo,value):
    if value.get('schema')!='qq-next-repository-swap/1' or value.get('repository')!=str(repo):raise ValueError('invalid repository switch journal')
    old=_safe(value['old']);candidate=_safe(value['candidate'])
    if old.parent!=repo.parent or candidate.parent!=repo.parent or not old.name.startswith('.'+repo.name+'.old-') or not candidate.name.startswith('.'+repo.name+'.compact-'):
        raise ValueError('switch paths escape repository generation boundary')
    for path in (repo,old,candidate):
        if path.exists() and hashlib.sha256((path/'OWNER.json').read_bytes()).hexdigest()!=value.get('owner_sha256'):
            raise ValueError('switch generation ownership differs; preserve every directory')
    return old,candidate


def recover(repo,*,cancelled=lambda:False):
    """Startup recovery rolls back uncommitted switches, never deletes payloads."""
    repo=_safe(repo);path=_journal(repo)
    if not path.exists():return _resume_cleanup(repo,cancelled=cancelled)
    # Recovery needs the same OS lock, but lease deliberately refuses a journal.
    # Permit this one scoped entry by checking the journal only after acquisition.
    with _recovery_lease(repo,cancelled=cancelled):
        if not path.exists():return _resume_cleanup(repo,cancelled=cancelled)
        value=json.loads(path.read_text(encoding='utf-8'));old,candidate=_checked_journal(repo,value)
        if value['phase']=='committed':
            if not repo.exists():raise ValueError('committed repository missing; old generation retained')
            receipt=json.loads((repo/'COMPACTION_RECEIPT.json').read_text(encoding='utf-8'))
            if receipt['descriptor_sha256']!=value['descriptor_sha256']:raise ValueError('committed generation differs')
            for identity,digest in value['descriptor_sha256'].items():
                if hashlib.sha256((repo/'points'/(identity+'.json')).read_bytes()).hexdigest()!=digest:
                    raise ValueError('committed point descriptor differs')
            path.unlink();return {'state':'committed','old':str(old),'cleanup':_resume_cleanup(repo,cancelled=cancelled)}
        if old.exists():
            if repo.exists():
                if candidate.exists():raise ValueError('ambiguous repository switch; preserve all generations')
                os.replace(repo,candidate)
            os.replace(old,repo)
        if not repo.exists():raise ValueError('repository recovery has no original generation')
        path.unlink();return {'state':'rolled_back','candidate':str(candidate)}


@contextmanager
def _recovery_lease(repo,*,cancelled=lambda:False):
    # lease checks the journal through a scoped thread flag, not by hiding it.
    old=getattr(_held,'recovering',False);_held.recovering=True
    try:
        with lease(repo,write=True,cancelled=cancelled):yield
    finally:_held.recovering=old


@guarded(write=True)
def switch(repo,candidate,*,cancelled=lambda:False,checkpoint=lambda phase:None,retire_after_verification=False):
    repo=Path(repo).resolve();candidate=_safe(candidate)
    if candidate.parent!=repo.parent or not candidate.name.startswith('.'+repo.name+'.compact-'):
        raise ValueError('candidate must be an owned sibling generation')
    from .backup import repository
    _dedicated(repo);_dedicated(candidate)
    repository(candidate)
    if (candidate/'INCOMPLETE.json').exists():raise ValueError('candidate compaction is incomplete')
    receipt=json.loads((candidate/'COMPACTION_RECEIPT.json').read_text(encoding='utf-8'))
    if (repo/'OWNER.json').read_bytes()!=(candidate/'OWNER.json').read_bytes():raise ValueError('repository identity changed')
    if hashlib.sha256((repo/'refs.json').read_bytes()).hexdigest()!=receipt['source_refs_sha256']:
        raise ValueError('source refs advanced; preserve new points and rebuild')
    if receipt['source_pack'] and _fingerprint(repo/'blocks.sqlite')!=receipt['source_pack']:
        raise ValueError('source blocks advanced; rebuild required')
    for identity,digest in receipt['descriptor_sha256'].items():
        if any(hashlib.sha256((p/'points'/(identity+'.json')).read_bytes()).hexdigest()!=digest for p in (repo,candidate)):
            raise ValueError('retained descriptor changed')
    if cancelled():raise InterruptedError('repository switch cancelled')
    old=repo.parent/('.'+repo.name+'.old-'+uuid.uuid4().hex)
    value={'schema':'qq-next-repository-swap/1','repository':str(repo),'old':str(old),'candidate':str(candidate),
           'owner_sha256':hashlib.sha256((repo/'OWNER.json').read_bytes()).hexdigest(),
           'descriptor_sha256':receipt['descriptor_sha256'],'phase':'ready'}
    path=_journal(repo);atomic_json(path,value);checkpoint('ready')
    os.replace(repo,old);value['phase']='old_moved';atomic_json(path,value);checkpoint('old_moved')
    os.replace(candidate,repo);value['phase']='new_active';atomic_json(path,value);checkpoint('new_active')
    atomic_json(repo/'CLEANUP_RECEIPT.json',{'schema':'qq-next-generation-cleanup/1','old':str(old),
      'owner_sha256':value['owner_sha256'],'descriptor_sha256':receipt['descriptor_sha256'],
      'state':'ready' if retire_after_verification else 'awaiting_acceptance','created':time.time()})
    value['phase']='committed';atomic_json(path,value);checkpoint('committed')
    path.unlink()
    return {'state':'committed','old':str(old),'repository':str(repo),'keep':receipt['keep']}


def _resume_cleanup(repo,*,cancelled=lambda:False):
    path=Path(repo)/'CLEANUP_RECEIPT.json'
    if not path.exists():return {'state':'clean'}
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema')!='qq-next-generation-cleanup/1':raise ValueError('invalid generation cleanup receipt')
    if value['state']=='awaiting_acceptance':return {'state':'clean','old_preserved':True,'requires_acceptance':True}
    if value['state']=='complete':return {'state':'clean'}
    if value['state'] not in ('ready','running'):raise ValueError('invalid generation cleanup state')
    try:return {'state':'clean','cleanup':discard_old(repo,value['old'],cancelled=cancelled)}
    except InterruptedError:raise
    except Exception as exc:
        if path.exists():
            current=json.loads(path.read_text(encoding='utf-8'));current['last_error']=str(exc);current['last_attempt']=time.time()
            atomic_json(path,current)
        return {'state':'clean','cleanup_pending':True,'error':str(exc)}


@guarded(write=True)
def discard_old(repo,old,*,cancelled=lambda:False):
    """Delete an accepted old generation only, with identity and path guards."""
    repo=_safe(repo);old=_safe(old)
    if old.parent!=repo.parent or not old.name.startswith('.'+repo.name+'.old-') or old==repo:
        raise ValueError('not an owned retired generation')
    receipt=json.loads((repo/'COMPACTION_RECEIPT.json').read_text(encoding='utf-8'))
    record_path=repo/'CLEANUP_RECEIPT.json'
    record=json.loads(record_path.read_text(encoding='utf-8')) if record_path.exists() else {}
    same_record=record.get('schema')=='qq-next-generation-cleanup/1' and record.get('old')==str(old) and record.get('descriptor_sha256')==receipt['descriptor_sha256'] and record.get('owner_sha256')==hashlib.sha256((repo/'OWNER.json').read_bytes()).hexdigest()
    resuming=same_record and record.get('state')=='running'
    if not old.exists():
        if same_record and record.get('state') in ('ready','running','complete'):
            record['state']='complete';atomic_json(record_path,record)
            return {'files_removed':record.get('planned_files',0),'logical_bytes_removed':record.get('planned_bytes',0),'resumed':True}
        raise ValueError('retired generation absent without an approved cleanup receipt')
    if (old/'OWNER.json').exists():
        if (old/'OWNER.json').read_bytes()!=(repo/'OWNER.json').read_bytes():raise ValueError('retired repository identity differs')
    elif not resuming or any(_walk_files(old)):raise ValueError('retired generation ownership missing')
    for identity,digest in receipt['descriptor_sha256'].items():
        descriptor=old/'points'/(identity+'.json')
        if resuming and not descriptor.exists():continue
        if hashlib.sha256(descriptor.read_bytes()).hexdigest()!=digest:raise ValueError('old retained point differs')
    files=[(path,_fingerprint(path)) for path in _walk_files(old)]
    roots={'OWNER.json','refs.json','blocks.sqlite','blocks.sqlite-wal','blocks.sqlite-shm','RETENTION.json',
      'RETIREMENT_RECEIPT.json','COMPACTION_RECEIPT.json','CLEANUP_RECEIPT.json'}
    for path,_ in files:
        parts=path.relative_to(old).parts
        if len(parts)==1 and parts[0] in roots:continue
        if len(parts)==2 and parts[0]=='points' and path.suffix=='.json':continue
        if len(parts)==3 and parts[0] in ('blobs','chunks') and len(parts[1])==2 and len(parts[2])==64 and parts[1]==parts[2][:2]:continue
        if len(parts)==2 and parts[0].startswith('staging-') and parts[1] in ('catalog.sqlite','assets.sqlite','catalog.sqlite-wal','catalog.sqlite-shm','assets.sqlite-wal','assets.sqlite-shm'):continue
        raise ValueError('unrecognized file in old repository; preserve it for review')
    files.sort(key=lambda item:(item[0].name=='OWNER.json',item[0].as_posix()))
    if not resuming:
        record={'schema':'qq-next-generation-cleanup/1','old':str(old),'state':'running','created':time.time(),
          'owner_sha256':hashlib.sha256((repo/'OWNER.json').read_bytes()).hexdigest(),'descriptor_sha256':receipt['descriptor_sha256'],
          'planned_files':len(files),'planned_bytes':sum(item[1][2] for item in files)}
        atomic_json(record_path,record)
    count=total=0
    for path,before in files:
        if cancelled():raise InterruptedError('retired repository cleanup cancelled')
        if _fingerprint(path)!=before:raise ValueError('retired file changed before cleanup')
        total+=before[2];path.unlink();count+=1
    for folder,dirs,names in os.walk(old,topdown=False):Path(folder).rmdir()
    record['state']='complete';record['finished']=time.time();atomic_json(record_path,record)
    return {'files_removed':record.get('planned_files',count),'logical_bytes_removed':record.get('planned_bytes',total),
      'physical_bytes':'measure volume free space; immutable files may still have other links','resumed':resuming}


@guarded(write=True)
def compact(repo,*,cancelled=lambda:False,progress=lambda phase,count:None):
    """Shared GUI/Agent/automatic maintenance; preserve old pack on any failure."""
    from .backup import verify_point
    repo=Path(repo).resolve();candidate=repo.parent/('.'+repo.name+'.compact-'+uuid.uuid4().hex)
    _dedicated(repo)
    pending=_resume_cleanup(repo,cancelled=cancelled)
    if pending.get('requires_acceptance') or pending.get('cleanup_pending'):
        raise ValueError('previous generation requires acceptance or cleanup before another compaction')
    required=(repo/'blocks.sqlite').stat().st_size if (repo/'blocks.sqlite').exists() else 0
    if shutil.disk_usage(repo).free<required*1.1+256*1024**2:raise OSError('insufficient free space for an independent repository generation')
    try:
        receipt=rebuild(repo,candidate,cancelled=cancelled,progress=progress)
        for index,identity in enumerate(receipt['keep'],1):
            verify_point(candidate,identity,cancelled=cancelled);progress('files_verified',index)
    except BaseException:
        # Only this invocation's fresh, owned generation is disposable. Never
        # clean an old generation or an interrupted switch in this path.
        if candidate.exists() and (candidate/'OWNER.json').read_bytes()==(repo/'OWNER.json').read_bytes():
            files=[(path,_fingerprint(path)) for path in _walk_files(candidate)]
            for path,before in files:
                if _fingerprint(path)!=before:raise ValueError('incomplete build changed before cleanup')
                path.unlink()
            for folder,dirs,names in os.walk(candidate,topdown=False):Path(folder).rmdir()
        raise
    result=switch(repo,candidate,cancelled=cancelled,retire_after_verification=True)
    result['cleanup']=discard_old(repo,result['old'],cancelled=cancelled)
    result['verification']=receipt
    return result

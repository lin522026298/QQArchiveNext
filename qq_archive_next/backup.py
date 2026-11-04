"""Append-only chunk repository, committed points, and independent restore.

No automatic garbage collection. A restore always creates a new directory.
The caller serializes backup with the state writer using WriterLock.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
import uuid
import zlib
from pathlib import Path

from .catalog import Catalog,canonical
from .config import load,inside
from .materialize import atomic_json

CHUNK=4*1024**2
SCHEMA='qq-next-backup-point/2'
LEGACY_SCHEMA='qq-next-backup-point/1'
UNIFIED_SCHEMA='qq-next-backup-point/3'


def repository(path,*,create=False):
    path=Path(path).resolve()
    if not path.exists():
        if not create:raise FileNotFoundError('backup repository absent')
        path.mkdir(parents=True)
        atomic_json(path/'OWNER.json',{'schema':'qq-next-backup-repository/1','id':uuid.uuid4().hex})
    owner=json.loads((path/'OWNER.json').read_text(encoding='utf-8'))
    if owner.get('schema')!='qq-next-backup-repository/1':raise ValueError('not an owned backup repository')
    return path


def sqlite_snapshot(source,target,cancelled,*,check=True):
    if target.exists():raise ValueError('snapshot target exists')
    original=sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
    copy=sqlite3.connect(target)
    try:
        def progress(status,remaining,total):
            if cancelled():raise InterruptedError('backup cancelled')
        original.backup(copy,pages=2048,progress=progress,sleep=0.01)
        if check and copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('backup SQLite structural failure')
    finally:copy.close();original.close()


def object_path(repo,digest):
    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('invalid chunk digest')
    return repo/'chunks'/digest[:2]/digest


def read_chunk(repo,item):
    payload=object_path(repo,item['sha256']).read_bytes()
    if len(payload)>CHUNK+1024:raise ValueError('oversized backup chunk')
    if payload[:1]==b'Z':
        decoder=zlib.decompressobj();data=decoder.decompress(payload[1:],CHUNK+1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('invalid compressed backup chunk')
    elif payload[:1]==b'R':data=payload[1:]
    else:raise ValueError('invalid backup chunk codec')
    if len(data)!=item['bytes'] or len(data)>CHUNK or hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('backup chunk hash failure')
    return data


def store_file(repo,path,cancelled):
    chunks=[];digest=hashlib.sha256();size=written=0
    with path.open('rb') as stream:
        while data:=stream.read(CHUNK):
            if cancelled():raise InterruptedError('backup cancelled')
            identity=hashlib.sha256(data).hexdigest();item={'sha256':identity,'bytes':len(data)}
            destination=object_path(repo,identity);destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                if read_chunk(repo,item)!=data:raise ValueError('existing repository object differs')
            else:
                compressed=zlib.compress(data,3)
                payload=b'Z'+compressed if len(compressed)+32<len(data) else b'R'+data
                temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.partial')
                with temporary.open('xb') as target:target.write(payload);target.flush();os.fsync(target.fileno())
                os.replace(temporary,destination);written+=len(payload)
            chunks.append(item);digest.update(data);size+=len(data)
    return {'bytes':size,'sha256':digest.hexdigest(),'chunks':chunks},written


def create_legacy(root,repo,*,include_keys=False,pin=False,cancelled=lambda:False):
    root,config=load(root);repo=repository(repo,create=True)
    if repo.is_relative_to(root) or root.is_relative_to(repo):raise ValueError('state and backup repository must be separate')
    staging=repo/('staging-'+uuid.uuid4().hex);staging.mkdir()
    # This owned transient SQLite image is discarded only after a committed point.
    files={'OWNER.json':root/'OWNER.json','config.private.json':root/'config.private.json',
           'viewer_manifest.json':root/'viewer_manifest.json','private/account_labels.private.json':root/'private/account_labels.private.json'}
    for source in config['sources']:files['viewer-'+source['account_key']+'.json']=root/('viewer-'+source['account_key']+'.json')
    for name in ('catalog.sqlite','assets.sqlite'):
        if (root/name).exists():
            sqlite_snapshot(root/name,staging/name,cancelled);files[name]=staging/name
    if 'catalog.sqlite' not in files:raise ValueError('no committed catalog to back up')
    external=[]
    if (staging/'assets.sqlite').exists():
        db=sqlite3.connect(staging/'assets.sqlite')
        try:
            for relative,expected in db.execute('SELECT relative,sha256 FROM asset'):
                path=inside(root,relative)
                if not path.is_file():raise ValueError('referenced media object missing')
                files[relative]=path
        finally:db.close()
    if config.get('legacy_publication'):external.append({'role':'optional_legacy_history','path':config['legacy_publication'],'included':False})
    if include_keys:
        for source in config['sources']:files[source['key_file']]=inside(root,source['key_file'])
    entries={};written=0
    for relative,path in sorted(files.items()):
        inside(root,relative)
        entries[relative],added=store_file(repo,path,cancelled);written+=added
    body={'schema':LEGACY_SCHEMA,'state_id':config['state_id'],'files':entries,'includes_source_keys':include_keys,
          'external_dependencies':external,'decoded_views_included':False,'cold_input_included':False}
    identity=hashlib.sha256(canonical(body).encode()).hexdigest()
    target=repo/'points'/(identity+'.json');target.parent.mkdir(parents=True,exist_ok=True)
    existed=target.exists()
    if not existed:atomic_json(target,{'id':identity,'created':time.time(),'body':body})
    refs_path=repo/'refs.json'
    try:refs=json.loads(refs_path.read_text(encoding='utf-8'))
    except FileNotFoundError:refs={'schema':'qq-next-backup-refs/1','recent':[],'pinned':[]}
    if refs.get('schema')!='qq-next-backup-refs/1':raise ValueError('invalid backup refs')
    if not refs['recent'] or refs['recent'][0]!=identity:
        refs['recent']=[identity]+[item for item in refs['recent'] if item!=identity][:19]
    if pin and identity not in refs['pinned']:refs['pinned'].append(identity)
    if not existed or not refs_path.exists() or pin:atomic_json(refs_path,refs)
    # Delete only files this invocation created in its private transient directory.
    for path in staging.iterdir():path.unlink()
    staging.rmdir()
    return {'id':identity,'no_op':existed,'new_object_bytes':written,'files':len(entries),
            'logical_bytes':sum(item['bytes'] for item in entries.values()),'includes_source_keys':include_keys}


def create(root,repo,*,include_keys=False,pin=False,cancelled=lambda:False):
    from .backup_blocks import Pack
    root,config=load(root);repo=repository(repo,create=True)
    if config['schema']=='qq-next-config/2':
        from .backup_unified import create as unified_create
        return unified_create(root,repo,include_keys=include_keys,pin=pin,cancelled=cancelled)
    if repo.is_relative_to(root) or root.is_relative_to(repo):raise ValueError('state and backup repository must be separate')
    try:_,previous=read_point(repo)
    except FileNotFoundError:previous=None
    if previous and previous['body']['schema']==SCHEMA and not (repo/'blocks.sqlite').exists():raise ValueError('the referenced backup block pack is missing')
    pack=Pack(repo,cancelled=cancelled)
    try:
        seeded=pack.seed_legacy(previous if previous and previous['body']['schema']==LEGACY_SCHEMA else None)
        previous_files=dict(point_files(repo,previous,cancelled=cancelled)) if previous and previous['body']['schema']==SCHEMA else {}
        staging=repo/('staging-'+uuid.uuid4().hex);staging.mkdir()
        files={'OWNER.json':root/'OWNER.json','config.private.json':root/'config.private.json',
               'viewer_manifest.json':root/'viewer_manifest.json','private/account_labels.private.json':root/'private/account_labels.private.json'}
        for source in config['sources']:files['viewer-'+source['account_key']+'.json']=root/('viewer-'+source['account_key']+'.json')
        for name in ('catalog.sqlite','assets.sqlite'):
            if (root/name).exists():
                sqlite_snapshot(root/name,staging/name,cancelled,check=False);files[name]=staging/name
        if 'catalog.sqlite' not in files:raise ValueError('no committed catalog to back up')
        media_specs={}
        if (staging/'assets.sqlite').exists():
            db=sqlite3.connect(staging/'assets.sqlite')
            try:
                for relative,sha,size in db.execute('SELECT relative,sha256,byte_count FROM asset'):
                    path=inside(root,relative)
                    if relative!='objects/'+sha[:2]+'/'+sha:raise ValueError('media CAS path differs from digest')
                    if not path.is_file() or path.stat().st_size!=size:raise ValueError('referenced media object missing or wrong size')
                    files[relative]=path
                    media_specs[relative]=(sha,size)
            finally:db.close()
        if include_keys:
            for source in config['sources']:files[source['key_file']]=inside(root,source['key_file'])
        entries={}
        for relative,path in sorted(files.items()):
            if cancelled():raise InterruptedError('backup cancelled')
            if relative not in media_specs:inside(root,relative)
            old=previous_files.get(relative)
            if relative in media_specs and old and (old['sha256'],old['bytes'])==media_specs[relative]:
                # Immutable CAS identity already exists in a committed point.
                # Preserve its original bytes without reopening 40k media files.
                # Explicit repository verification/restore reads the payloads.
                entries[relative]=old
            else:
                entries[relative]=pack.file(path,block_size=4096 if relative.endswith('.sqlite') else CHUNK,previous=old)
                if relative in media_specs and (entries[relative]['sha256'],entries[relative]['bytes'])!=media_specs[relative]:raise ValueError('new media bytes differ from their CAS identity')
        external=[]
        if config.get('legacy_publication'):external.append({'role':'optional_legacy_history','path':config['legacy_publication'],'included':False})
        tree=pack.file_tree(entries,previous['body']['files'] if previous and previous['body']['schema']==SCHEMA else None)
        body={'schema':SCHEMA,'state_id':config['state_id'],'files':tree,'includes_source_keys':include_keys,
          'external_dependencies':external,'decoded_views_included':False,'cold_input_included':False,
          'copy_validation':'sqlite_online_backup_and_block_file_hashes','semantic_verification':'restore_required'}
        identity=hashlib.sha256(canonical(body).encode()).hexdigest();target=repo/'points'/(identity+'.json');target.parent.mkdir(parents=True,exist_ok=True)
        existed=target.exists();pack.commit()
        if not existed:atomic_json(target,{'id':identity,'created':time.time(),'body':body})
        refs_path=repo/'refs.json'
        try:refs=json.loads(refs_path.read_text(encoding='utf-8'))
        except FileNotFoundError:refs={'schema':'qq-next-backup-refs/1','recent':[],'pinned':[]}
        if refs.get('schema')!='qq-next-backup-refs/1':raise ValueError('invalid backup refs')
        if not refs['recent'] or refs['recent'][0]!=identity:refs['recent']=[identity]+[item for item in refs['recent'] if item!=identity][:19]
        if pin and identity not in refs['pinned']:refs['pinned'].append(identity)
        if not existed or not refs_path.exists() or pin:atomic_json(refs_path,refs)
        for path in staging.iterdir():path.unlink()
        staging.rmdir()
        return {'id':identity,'schema':SCHEMA,'no_op':existed,'new_object_bytes':pack.new_bytes,
          'new_blocks':pack.added,'legacy_blocks_seeded':seeded,'files':tree['count'],'logical_bytes':tree['logical_bytes'],
          'includes_source_keys':include_keys,'storage_accounting':'encoded block payload bytes; SQLite allocation is separate'}
    finally:pack.close()


def read_point(repo,identity=None):
    repo=repository(repo)
    if (repo/'INCOMPLETE.json').exists():raise ValueError('portable export is incomplete')
    if identity is None:identity=json.loads((repo/'refs.json').read_text(encoding='utf-8'))['recent'][0]
    object_path(repo,identity)  # strict digest validation
    point=json.loads((repo/'points'/(identity+'.json')).read_text(encoding='utf-8'))
    if point.get('id')!=identity or hashlib.sha256(canonical(point['body']).encode()).hexdigest()!=identity or point['body'].get('schema') not in (SCHEMA,LEGACY_SCHEMA,UNIFIED_SCHEMA):
        raise ValueError('backup point hash/schema mismatch')
    return repo,point


def point_files(repo,point,*,cancelled=lambda:False):
    if point['body']['schema']==LEGACY_SCHEMA:
        yield from point['body']['files'].items();return
    from .backup_blocks import Pack
    pack=Pack(repo,readonly=True,cancelled=cancelled)
    try:yield from pack.files(point['body']['files'])
    finally:pack.close()


def verify_point(repo,identity=None,*,cancelled=lambda:False):
    """Reconstruct/hash every file without allocating another full restored copy."""
    repo,point=read_point(repo,identity)
    from .backup_blocks import Pack
    pack=Pack(repo,readonly=True,cancelled=cancelled) if point['body']['schema'] in (SCHEMA,UNIFIED_SCHEMA) else None
    count=total=0
    try:
        for relative,entry in point_files(repo,point,cancelled=cancelled):
            if (repo/'PORTABLE_RECEIPT.json').is_file() and relative.startswith('application/'):
                from .history import sha_file
                if sha_file(inside(repo,relative),cancelled)!=(entry['bytes'],entry['sha256']):raise ValueError('portable recovery program hash mismatch')
            digest=hashlib.sha256();size=0
            if 'blob' in entry:
                from .backup_unified import blob_data
                chunks=blob_data(repo,entry)
            else:chunks=pack.data(entry['root']) if pack else (read_chunk(repo,item) for item in entry['chunks'])
            for data in chunks:
                if cancelled():raise InterruptedError('backup verification cancelled')
                digest.update(data);size+=len(data)
            if size!=entry['bytes'] or digest.hexdigest()!=entry['sha256']:raise ValueError('reconstructed file hash mismatch')
            count+=1;total+=size
        return {'point':point['id'],'files_reconstructed_and_hashed':count,'bytes_verified':total}
    finally:
        if pack:pack.close()


def restore(repo,target,*,identity=None,deep=True,cancelled=lambda:False,hardlink_immutable=False):
    repo,point=read_point(repo,identity);target=Path(target).resolve()
    if target.exists():raise ValueError('restore requires a new destination')
    portable_child=False
    if target==repo/'Recovered' and (repo/'PORTABLE_RECEIPT.json').is_file():
        receipt=json.loads((repo/'PORTABLE_RECEIPT.json').read_text(encoding='utf-8'))
        portable_child=receipt.get('schema')=='qq-next-portable-export/1' and receipt.get('point')==point['id']
    if (target.is_relative_to(repo) or repo.is_relative_to(target)) and not portable_child:raise ValueError('restore/repository overlap')
    target.mkdir(parents=True)
    atomic_json(target/'RESTORING.json',{'point':point['id'],'state':'uncommitted'})
    from .backup_blocks import Pack
    pack=Pack(repo,readonly=True,cancelled=cancelled) if point['body']['schema'] in (SCHEMA,UNIFIED_SCHEMA) else None
    restored_files=linked_bytes=restored_bytes=0;last_file_progress=time.monotonic()
    try:
        for relative,entry in point_files(repo,point,cancelled=cancelled):
            path=inside(target,relative);path.parent.mkdir(parents=True,exist_ok=True)
            digest=hashlib.sha256();size=0
            if 'blob' in entry:
                from .backup_unified import blob_data,blob_path
                if entry['blob']!=entry['sha256']:raise ValueError('invalid raw blob identity')
                chunks=blob_data(repo,entry)
            else:chunks=pack.data(entry['root']) if pack else (read_chunk(repo,item) for item in entry['chunks'])
            linked=False
            if hardlink_immutable and 'blob' in entry:
                try:os.link(blob_path(repo,entry['blob']),path);linked=True
                except OSError:pass
            stream=None if linked else path.open('xb',buffering=1024**2)
            try:
                for data in chunks:
                    if cancelled():raise InterruptedError('restore cancelled')
                    if stream:stream.write(data)
                    digest.update(data);size+=len(data)
                    if time.monotonic()-last_file_progress>=2:
                        atomic_json(target/'RESTORING.json',{'point':point['id'],'state':'uncommitted','phase':'file_hashes',
                          'files_verified':restored_files,'bytes_read':restored_bytes+size})
                        last_file_progress=time.monotonic()
                if stream:stream.flush();os.fsync(stream.fileno())
            finally:
                if stream:stream.close()
            if digest.hexdigest()!=entry['sha256'] or size!=entry['bytes']:raise ValueError('restored file hash mismatch')
            if linked:linked_bytes+=size
            restored_files+=1;restored_bytes+=size
    finally:
        if pack:pack.close()
    root,config=load(target,allow_restoring=True)
    # A restored reading copy does not silently attach to live QQ or start capture.
    config['auto_capture']=False;config['restored_from']=point['id']
    atomic_json(target/'config.private.json',config)
    catalog=Catalog(target/'catalog.sqlite',readonly=not deep)
    try:
        if not deep and (catalog.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(catalog.db.execute('PRAGMA foreign_key_check'))):raise ValueError('restored catalog integrity failure')
        last_progress=[0.0]
        def progress(phase,rows):
            if time.monotonic()-last_progress[0]>=2:
                atomic_json(target/'RESTORING.json',{'point':point['id'],'state':'uncommitted','phase':phase,'rows_verified':rows})
                last_progress[0]=time.monotonic()
        verification=catalog.verify(cancelled=cancelled,progress=progress) if deep else catalog.summary()
    finally:catalog.close()
    if (target/'assets.sqlite').exists():
        from .media import verify
        verification['media']=verify(target,cancelled=cancelled)
    if config['schema']=='qq-next-config/2':
        from .history import verify
        verification['history']=verify(target,config,cancelled=cancelled,progress=progress)
    receipt={'schema':'qq-next-restore-receipt/1','point':point['id'],'verified':time.time(),
      'file_hashes_verified':restored_files,'deep_semantic_verification':deep,
      'verification':verification,'capture_disabled':True,'source_keys_included':point['body']['includes_source_keys'],
      'external_dependencies':point['body']['external_dependencies'],'hardlinked_bytes':linked_bytes,
      'immutable_reuse_requested':bool(hardlink_immutable),'physical_independence':not bool(linked_bytes)}
    atomic_json(target/'RESTORE_RECEIPT.json',receipt)
    (target/'RESTORING.json').unlink()
    return receipt

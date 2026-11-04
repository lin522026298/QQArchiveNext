"""Self-contained, immutable history packages and their original evidence."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .config import inside
from .materialize import atomic_json


def sha_file(path,cancelled=lambda:False):
    digest=hashlib.sha256();size=0
    with Path(path).open('rb',buffering=8*1024**2) as stream:
        while data:=stream.read(8*1024**2):
            if cancelled():raise InterruptedError('history operation cancelled')
            size+=len(data);digest.update(data)
    return size,digest.hexdigest()


def cas_digest(relative):
    parts=Path(relative).parts
    if len(parts)>=4 and parts[-4:-2]==('objects','sha256') and len(parts[-1])==64 and parts[-2]==parts[-1][:2] and all(c in '0123456789abcdef' for c in parts[-1]):return parts[-1]
    return None


def copy_tree(source,target,inventory,*,cancelled=lambda:False,progress=None):
    """Hash every source byte; only immutable CAS bytes may share a file ID."""
    source=Path(source).resolve(strict=True);target=Path(target).resolve();inventory=Path(inventory)
    if target.exists() or inventory.exists():raise ValueError('history staging requires fresh targets')
    if source.is_relative_to(target) or target.is_relative_to(source):raise ValueError('history source/target overlap')
    target.mkdir(parents=True);inventory.parent.mkdir(parents=True,exist_ok=True)
    files=logical=linked=0;started=last=time.monotonic()
    with inventory.open('x',encoding='utf-8',newline='\n') as listing:
        for folder,directories,names in os.walk(source,followlinks=False):
            directories.sort();names.sort()
            if any((Path(folder)/name).is_symlink() for name in directories):raise ValueError('history contains a directory symlink')
            for name in names:
                if cancelled():raise InterruptedError('history staging cancelled')
                path=Path(folder)/name
                if path.is_symlink():raise ValueError('history contains a symlink')
                relative=path.relative_to(source).as_posix();destination=inside(target,relative);destination.parent.mkdir(parents=True,exist_ok=True)
                before=path.stat();expected=cas_digest(relative)
                size,digest=sha_file(path,cancelled)
                if expected and expected!=digest:raise ValueError('history CAS bytes differ from their identity')
                used_link=False
                if expected and before.st_dev==target.stat().st_dev:
                    try:os.link(path,destination);used_link=True
                    except OSError:pass
                if not used_link:
                    shutil.copyfile(path,destination)
                    if sha_file(destination,cancelled)!=(size,digest):raise ValueError('history destination differs')
                after=path.stat()
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('immutable history input changed during staging')
                if used_link and not os.path.samefile(path,destination):raise ValueError('history link identity changed')
                listing.write(json.dumps({'path':relative,'bytes':size,'sha256':digest,'cas':bool(expected)},ensure_ascii=False,sort_keys=True)+'\n')
                files+=1;logical+=size;linked+=size if used_link else 0
                now=time.monotonic()
                if progress and now-last>=2:progress({'files':files,'logical_bytes':logical,'hardlinked_bytes':linked,'seconds':round(now-started,3)});last=now
        listing.flush();os.fsync(listing.fileno())
    return {'files':files,'logical_bytes':logical,'hardlinked_bytes':linked,'copied_bytes':logical-linked,
      'inventory_sha256':sha_file(inventory)[1],'seconds':round(time.monotonic()-started,3)}


def stage(old_state,destination,*,report_dir):
    old_state=Path(old_state).resolve(strict=True);destination=Path(destination).resolve();report_dir=Path(report_dir).resolve()
    if destination.exists():raise ValueError('unified state already exists')
    config=json.loads((old_state/'config.private.json').read_text(encoding='utf-8'))
    publication=Path(config['legacy_publication']).resolve(strict=True);source=publication.parent
    document=json.loads(publication.read_text(encoding='utf-8'))
    if document.get('schema')!='qq-canonical-publication/2' or document.get('status')!='verified':raise ValueError('expected a verified v2 publication')
    labels_path=inside(source,document['private']['account_labels_sidecar'])
    labels=json.loads(labels_path.read_text(encoding='utf-8-sig'))
    label_map={str(v['account_label']):v['account_key'] for v in labels['accounts']}
    accounts={v['account_key']:v for v in document['accounts']}
    legacy=[]
    for number,native in enumerate(config['sources'],1):
        key=label_map.get(str(native.get('account_label')))
        if not key or key not in accounts:raise ValueError('account labels do not prove a unique old/current binding')
        item=accounts[key];manifest=inside(source,item['archive_manifest']);archive=json.loads(manifest.read_text(encoding='utf-8'))
        db=(manifest.parent/archive['message_store']['path']).resolve(strict=True)
        if not db.is_relative_to(source):raise ValueError('legacy message store escapes publication')
        legacy.append({'id':'legacy-v2-a'+str(number),'account_key':native['account_key'],'legacy_account_key':key,
          'manifest':'history/v2/'+manifest.relative_to(source).as_posix(),'message_store':'history/v2/'+db.relative_to(source).as_posix(),
          'messages':item['messages']['logical_messages'],'observations':item['messages']['observations']})
    if len({v['legacy_account_key'] for v in legacy})!=len(legacy) or len(legacy)!=len(accounts):raise ValueError('legacy/current account coverage differs')
    destination.mkdir(parents=True);report_dir.mkdir(parents=True,exist_ok=True)
    atomic_json(destination/'BUILDING.json',{'schema':'qq-next-unified-build/1','state':'staging_history','started':time.time()})
    state_id=str(uuid.uuid4());atomic_json(destination/'OWNER.json',{'schema':'qq-next-state/1','id':state_id})
    original_id=config['state_id'];config=dict(config);config['schema']='qq-next-config/2';config['state_id']=state_id
    config['auto_capture']=False;config.pop('legacy_publication',None);config['legacy_sources']=legacy
    config['integration_origin_state']=original_id
    config['history_packages']=[];config['recovery_packages']=[]
    for native in config['sources']:
        native.pop('legacy_archive',None)
        original=inside(old_state,native['key_file']);target=inside(destination,native['key_file']);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(original,target)
        if sha_file(original)!=sha_file(target):raise ValueError('saved key copy differs')
    atomic_json(destination/'config.private.json',config)
    manifest_hash=sha_file(publication)[1]
    def progress(value):atomic_json(report_dir/'history-stage-progress.private.json',value)
    result=copy_tree(source,destination/'history/v2',destination/'history/v2_inventory.jsonl',progress=progress)
    if sha_file(publication)[1]!=manifest_hash:raise ValueError('source publication changed')
    config['history_packages']=[{'id':'legacy-v2','root':'history/v2','inventory':'history/v2_inventory.jsonl',
      'publication_sha256':manifest_hash,**result}]
    atomic_json(destination/'config.private.json',config)
    recovery=old_state.parent/'recovery-input-20261103'
    if recovery.is_dir():
        result_recovery=copy_tree(recovery,destination/'recovery/current',destination/'recovery/current_inventory.jsonl',progress=lambda v:atomic_json(report_dir/'recovery-stage-progress.private.json',v))
        config['recovery_packages']=[{'id':'current-original-input','root':'recovery/current','inventory':'recovery/current_inventory.jsonl',**result_recovery}]
        atomic_json(destination/'config.private.json',config)
    atomic_json(report_dir/'history-stage.private.json',{'schema':'qq-next-history-stage/1','history':result,'account_mapping':legacy,'recovery':config['recovery_packages']})
    return {'history':result,'legacy_accounts':len(legacy),'message_rows':sum(v['messages'] for v in legacy)}


def package_files(root,config):
    """Authenticated inventories enumerate every frozen original, including raw inputs."""
    for package in config.get('history_packages',[])+config.get('recovery_packages',[]):
        inventory=inside(root,package['inventory'])
        if sha_file(inventory)[1]!=package['inventory_sha256']:raise ValueError('package inventory digest differs')
        yield package['inventory'],inventory,None
        base=inside(root,package['root']);seen=set();count=total=0
        with inventory.open(encoding='utf-8') as listing:
            for line in listing:
                item=json.loads(line);name=item['path'];path=inside(base,name)
                if name in seen:raise ValueError('duplicate original inventory path')
                seen.add(name);count+=1;total+=item['bytes']
                yield package['root']+'/'+name,path,item
        if count!=package['files'] or total!=package['logical_bytes']:raise ValueError('package inventory totals differ')


def verify(root,config,*,cancelled=lambda:False,progress=None):
    """Check all frozen bytes, original observations, bindings and media metadata."""
    import sqlite3
    count=total=0;last=time.monotonic();source_digests={};needed={s['message_store'] for s in config.get('legacy_sources',[])}
    for relative,path,item in package_files(root,config):
        if cancelled():raise InterruptedError('history verification cancelled')
        actual=sha_file(path,cancelled)
        if relative in needed:source_digests[relative]=actual[1]
        if item and actual!=(item['bytes'],item['sha256']):raise ValueError('frozen original bytes differ: '+relative)
        count+=1;total+=actual[0]
        if progress and time.monotonic()-last>2:progress('original_files',count);last=time.monotonic()
    accounts=[]
    catalog=sqlite3.connect((Path(root)/'catalog.sqlite').as_uri()+'?mode=ro',uri=True)
    from . import codec
    catalog.create_function('historic_row_key',1,lambda n:hashlib.sha256(codec.encode([int(n)]).encode()).digest(),deterministic=True)
    catalog.set_progress_handler(lambda:int(cancelled()),10000)
    try:
        for source in config.get('legacy_sources',[]):
            path=inside(root,source['message_store'])
            db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
            try:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('historic source integrity failed')
                messages=db.execute('SELECT count(*) FROM logical_messages').fetchone()[0]
                observations=db.execute('SELECT count(*) FROM message_provenance').fetchone()[0]
                runs=db.execute('SELECT count(*) FROM source_runs').fetchone()[0]
                represented=catalog.execute('SELECT count(*) FROM message_head WHERE source_id=?',(source['id'],)).fetchone()[0]
                if messages!=source['messages'] or observations!=source['observations'] or represented!=messages:raise ValueError('historic message/provenance coverage differs')
                binding=catalog.execute('SELECT account_key,kind,binding FROM source WHERE id=?',(source['id'],)).fetchone()
                if binding!=(source['account_key'],'publication',source_digests[source['message_store']]):raise ValueError('historic source bytes/account binding differs')
                catalog.execute('ATTACH DATABASE ? AS historic',(path.as_uri()+'?mode=ro&immutable=1',))
                try:
                    if progress:progress('historic_message_identity',0)
                    missing=catalog.execute('''SELECT 1 FROM historic.logical_messages l LEFT JOIN main.message_head h
                      ON h.source_id=? AND h.source_table=CASE l.source_table WHEN 'c2c_msg_table' THEN 'c2c_messages' WHEN 'group_msg_table' THEN 'group_messages' ELSE l.source_table END
                      AND h.row_key=historic_row_key(l.published_msg_id)
                      LEFT JOIN main.message_version v ON v.version_id=h.version_id
                      WHERE h.version_id IS NULL OR v.source_msg_id!=CAST(l.published_msg_id AS TEXT) LIMIT 1''',(source['id'],)).fetchone()
                    if missing:raise ValueError('historic published record has no exact unified identity')
                finally:catalog.execute('DETACH DATABASE historic')
                accounts.append({'source':source['id'],'messages':messages,'original_observations':observations,'source_runs':runs,'unified_heads':represented,'all_original_message_ids_matched':True})
            finally:db.close()
    except sqlite3.OperationalError as exc:
        if cancelled() and getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT:raise InterruptedError('history verification cancelled') from exc
        raise
    finally:catalog.close()
    return {'original_files_hashed':count,'original_bytes_hashed':total,'accounts':accounts,'external_history_dependencies':[]}

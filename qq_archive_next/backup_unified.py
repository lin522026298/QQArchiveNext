"""Complete points: page blocks for live databases, whole frozen originals.

Raw blobs can share immutable CAS bytes on the same volume. They remain normal
files, so a plain folder copy to a different device is self contained.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from .config import inside,load
from .history import package_files,sha_file,cas_digest
from .materialize import atomic_json
from .catalog import canonical
from .repository_gc import guarded

SCHEMA='qq-next-backup-point/3'


def blob_path(repo,digest):
    from .backup import object_path
    object_path(Path(repo),digest)
    return Path(repo)/'blobs'/digest[:2]/digest


def store_blob(repo,path,*,expected=None,link=False,cancelled=lambda:False):
    before=path.stat();size,digest=sha_file(path,cancelled)
    if expected and (size,digest)!=(expected['bytes'],expected['sha256']):raise ValueError('original bytes differ from inventory')
    destination=blob_path(repo,digest);destination.parent.mkdir(parents=True,exist_ok=True)
    added=linked=0
    if destination.exists():
        if destination.stat().st_size!=size:raise ValueError('backup blob size differs')
        if not os.path.samefile(path,destination) and sha_file(destination,cancelled)!=(size,digest):raise ValueError('existing backup blob hash differs')
    else:
        temporary=destination.with_name('.'+uuid.uuid4().hex+'.partial')
        try:
            if link and path.stat().st_dev==destination.parent.stat().st_dev:
                try:os.link(path,temporary);linked=size
                except OSError:pass
            if not temporary.exists():
                shutil.copyfile(path,temporary)
                if sha_file(temporary,cancelled)!=(size,digest):raise ValueError('backup blob copy differs')
                added=size
                with temporary.open('r+b') as stream:os.fsync(stream.fileno())
            os.replace(temporary,destination)
        finally:temporary.unlink(missing_ok=True)
    after=path.stat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('frozen input changed during backup')
    return {'bytes':size,'sha256':digest,'blob':digest},added,linked


def original_specs(root,config,assets_path=None):
    for relative,path,item in package_files(root,config):yield relative,path,item,bool(item and item.get('cas'))
    if assets_path and assets_path.is_file():
        db=sqlite3.connect(assets_path.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            for relative,sha,size in db.execute('SELECT relative,sha256,byte_count FROM asset'):
                if relative!='objects/'+sha[:2]+'/'+sha:raise ValueError('current media CAS identity differs')
                yield relative,inside(root,relative),{'bytes':size,'sha256':sha},True
        finally:db.close()
    application=config.get('application_directory')
    if application:
        base=inside(root,application)
        for folder,directories,names in os.walk(base,followlinks=False):
            if any((Path(folder)/n).is_symlink() for n in directories+names):raise ValueError('application package contains symlink')
            for name in sorted(names):
                path=Path(folder)/name;yield path.relative_to(root).as_posix(),path,None,False


def create(root,repo,*,include_keys=False,pin=False,cancelled=lambda:False):
    from .backup import repository,read_point,point_files
    from .backup_blocks import Pack
    from .backup_input import owned_input
    root,config=load(root);repo=repository(repo,create=True)
    if repo.is_relative_to(root) or root.is_relative_to(repo):raise ValueError('state/repository overlap')
    try:_,previous=read_point(repo)
    except FileNotFoundError:previous=None
    if previous and previous['body']['schema']!=SCHEMA:raise ValueError('unified points require a dedicated backup repository')
    old=dict(point_files(repo,previous,cancelled=cancelled)) if previous else {}
    staging=repo/('staging-'+uuid.uuid4().hex);staging.mkdir()
    pack=Pack(repo,cancelled=cancelled);entries={};added=linked=0;inputs={};assets_input=None
    try:
        names={'OWNER.json','config.private.json','viewer_manifest.json','private/account_labels.private.json'}
        names.update('viewer-'+s['account_key']+'.json' for s in config['sources'])
        names.update(s['key_file'] for s in config['sources'])
        files={name:inside(root,name) for name in names}
        for name in ('catalog.sqlite','assets.sqlite'):
            if (root/name).exists():
                with owned_input(root,name,staging,cancelled) as (path,validation):
                    entries[name]=pack.file(path,block_size=4096,previous=old.get(name));inputs[name]=validation
                    if name=='assets.sqlite':assets_input=path
        if 'catalog.sqlite' not in entries:raise ValueError('no committed catalog')
        for name,path in sorted(files.items()):
            if name not in entries:entries[name]=pack.file(path,block_size=4*1024**2,previous=old.get(name))
        for name,path,expected,can_link in original_specs(root,config,assets_input):
            if cancelled():raise InterruptedError('unified backup cancelled')
            if name in entries:raise ValueError('duplicate backed up path')
            prior=old.get(name)
            if expected and prior and 'blob' in prior and (prior['bytes'],prior['sha256'])==(expected['bytes'],expected['sha256']):
                if path.stat().st_size!=expected['bytes'] or blob_path(repo,prior['blob']).stat().st_size!=expected['bytes']:raise ValueError('reused original missing or wrong size')
                entries[name]=prior
            else:
                entries[name],a,l=store_blob(repo,path,expected=expected,link=can_link,cancelled=cancelled);added+=a;linked+=l
        for source in config.get('legacy_sources',[]):
            if any(source[name] not in entries for name in ('manifest','message_store')):raise ValueError('unified backup omits a historic dependency')
        tree=pack.file_tree(entries,previous['body']['files'] if previous else None)
        body={'schema':SCHEMA,'state_id':config['state_id'],'files':tree,'includes_source_keys':True,
          'external_dependencies':[],'cold_input_included':True,'history_included':True,'decoded_views_included':False,
          'copy_validation':'all_frozen_original_hashes_and_reserved_main_or_online_snapshot','semantic_verification':'restore_required'}
        identity=hashlib.sha256(canonical(body).encode()).hexdigest();target=repo/'points'/(identity+'.json');target.parent.mkdir(parents=True,exist_ok=True)
        existed=target.exists();pack.commit()
        if not existed:atomic_json(target,{'id':identity,'created':time.time(),'body':body})
        refs_path=repo/'refs.json'
        refs=json.loads(refs_path.read_text(encoding='utf-8')) if refs_path.exists() else {'schema':'qq-next-backup-refs/1','recent':[],'pinned':[]}
        if refs.get('schema')!='qq-next-backup-refs/1':raise ValueError('invalid backup refs')
        changed=not refs['recent'] or refs['recent'][0]!=identity or (pin and identity not in refs['pinned'])
        refs['recent']=[identity]+[item for item in refs['recent'] if item!=identity][:19]
        if pin and identity not in refs['pinned']:refs['pinned'].append(identity)
        if changed:atomic_json(refs_path,refs)
        for path in staging.iterdir():path.unlink()
        staging.rmdir()
        return {'id':identity,'schema':SCHEMA,'no_op':existed,'files':tree['count'],'logical_bytes':tree['logical_bytes'],
          'new_object_bytes':pack.new_bytes+added,'new_blocks':pack.added,'hardlinked_bytes':linked,
          'includes_source_keys':True,'external_dependencies':[],
          'sqlite_inputs':inputs,'temporary_snapshot_bytes':sum(v['temporary_bytes'] for v in inputs.values()),
          'storage_accounting':'hardlinks share physical bytes on this volume; ordinary cross-device copies are independent'}
    finally:pack.close()


def blob_data(repo,entry):
    if entry.get('blob')!=entry['sha256']:raise ValueError('blob reference differs from file digest')
    with blob_path(repo,entry['blob']).open('rb') as stream:
        while block:=stream.read(8*1024**2):yield block


@guarded()
def export(repo,target,*,identity=None,cancelled=lambda:False,application=None):
    """A fixed portable recovery folder; no live database is copied by shutil."""
    from .backup import read_point,point_files,sqlite_snapshot
    repo,point=read_point(repo,identity);target=Path(target).resolve()
    if point['body']['schema']!=SCHEMA:raise ValueError('portable export requires a unified point')
    if target.exists() or target.is_relative_to(repo) or repo.is_relative_to(target):raise ValueError('export requires a fresh separate directory')
    target.mkdir();atomic_json(target/'INCOMPLETE.json',{'point':point['id']})
    shutil.copyfile(repo/'OWNER.json',target/'OWNER.json')
    sqlite_snapshot(repo/'blocks.sqlite',target/'blocks.sqlite',cancelled)
    (target/'points').mkdir();shutil.copyfile(repo/'points'/(point['id']+'.json'),target/'points'/(point['id']+'.json'))
    atomic_json(target/'refs.json',{'schema':'qq-next-backup-refs/1','recent':[point['id']],'pinned':[point['id']]})
    count=logical=linked=copied=0;seen=set()
    for name,entry in point_files(repo,point,cancelled=cancelled):
        if 'blob' not in entry:continue
        sha=entry['blob']
        if sha not in seen:
            _,a,l=store_blob(target,blob_path(repo,sha),expected=entry,link=True,cancelled=cancelled)
            seen.add(sha);count+=1;logical+=entry['bytes'];copied+=a;linked+=l
        if name.startswith('application/'):
            path=inside(target,name);path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(blob_path(target,sha),path)
    if not (target/'application/QQArchiveNext.exe').is_file():raise ValueError('point lacks portable recovery program')
    (target/'Restore.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\necho Restoring and verifying all records. Please wait.\r\nstart /wait "" "application\\QQArchiveNext.exe" --report "%~dp0Restore-result.private.json" restore --repository "%~dp0." --destination "%~dp0Recovered" --reuse-immutable\r\necho Finished. See Restore-result.private.json for the result.\r\npause\r\n',encoding='ascii')
    (target/'Open.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\n"application\\QQArchiveNext.exe" --state "%~dp0Recovered" gui\r\n',encoding='ascii')
    (target/'Verify.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\necho Verifying every file. Please wait.\r\nstart /wait "" "application\\QQArchiveNext.exe" --report "%~dp0Verify-result.private.json" verify-backup --repository "%~dp0."\r\necho Finished. See Verify-result.private.json for the result.\r\npause\r\n',encoding='ascii')
    (target/'使用说明.txt').write_text('请把整个目录复制到有足够空间、支持大文件的磁盘或 Windows x64 电脑。先运行 Verify.cmd 校验，再运行 Restore.cmd；全量恢复可能耗时较长，Recovered/RESTORING.json 记录进度。成功后存在 Recovered/RESTORE_RECEIPT.json，再运行 Open.cmd 阅读。结果分别保存在 Verify-result.private.json 和 Restore-result.private.json。\n恢复不覆盖既有 Recovered 目录；失败目录应保留以便检查。恢复后默认关闭采集，阅读无需 Python 或 QQ 登录。\nRestore.cmd 使用同卷不可变文件复用，减少原始资料的重复空间；消息数据库与配置正常重建，全部文件仍逐个校验。无法硬链接时自动普通复制；手工 restore 省略 --reuse-immutable 时全部普通复制，需要相应空间。\n包内包含原始聊天和私密恢复材料，请妥善保管。同盘硬链接共享物理字节，跨盘普通复制后不依赖原路径；physical_independence 表示恢复目录是否与该包共享物理字节，同盘复用不代表另一份物理备份。\n',encoding='utf-8-sig')
    from .portable import recovery_tool,launchers,supports_assistant
    tool=recovery_tool(target,application)
    if supports_assistant(target/tool):
        launchers(target,tool)
        (target/'开始使用.txt').write_text('复制整个文件夹到新电脑。双击 Start.cmd 或 打开档案.cmd；首次会进入验证与恢复助手，恢复完成后直接打开。\n恢复只写新目录，可安全取消；未完成目录保留标记。恢复后默认关闭采集，登录自启也不会自动开启。\n需要命令行时仍可使用 Verify.cmd 和 Restore.cmd，结果保存为 JSON。\n保留原备份点内程序版本；recovery-tool 若存在，是新版恢复与阅读工具，也需要一起复制。\n',encoding='utf-8-sig')
    receipt={'schema':'qq-next-portable-export/1','point':point['id'],'unique_blobs':count,'blob_logical_bytes':logical,
      'recovery_tool':tool,'graphical_assistant':supports_assistant(target/tool),
      'hardlinked_bytes':linked,'copied_blob_bytes':copied,'fixed_point':True,'external_dependencies':[]}
    atomic_json(target/'PORTABLE_RECEIPT.json',receipt);(target/'INCOMPLETE.json').unlink()
    return receipt

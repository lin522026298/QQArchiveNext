"""Portable entry points; preserve the original point's application version."""
import json
import shutil
import sys
from pathlib import Path
from .config import inside
from .materialize import file_hash


def supports_assistant(folder):
    try:return 'portable_assistant' in json.loads((Path(folder)/'release_verification.json').read_text(encoding='utf-8')).get('features',[])
    except (OSError,ValueError):return False


def recovery_tool(target,application=None):
    target=Path(target)
    if supports_assistant(target/'application'):return 'application'
    if application is None and getattr(sys,'frozen',False):application=Path(sys.executable).parents[2]
    if application is None:return 'application'
    application=Path(application).resolve(strict=True)
    if not supports_assistant(application):return 'application'
    metadata=json.loads((application/'release_verification.json').read_text(encoding='utf-8'))
    entries={item['path']:item for item in metadata['files']}
    destination=target/'recovery-tool';destination.mkdir()
    for name in [*entries,'release_verification.json']:
        source=inside(application,name);copied=inside(destination,name)
        if source.is_symlink():raise ValueError('recovery program contains a symlink')
        digest=file_hash(source);size=source.stat().st_size
        if name in entries and (digest,size)!=(entries[name]['sha256'],entries[name]['bytes']):raise ValueError('recovery program hash differs')
        copied.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,copied)
        if file_hash(copied)!=digest:raise ValueError('recovery program copy differs')
    return 'recovery-tool'


def launchers(target,tool='application'):
    target=Path(target)
    exe=tool.replace('/','\\')+'\\QQArchiveNext.exe'
    gui='@echo off\r\ncd /d "%~dp0"\r\nstart "" "'+exe+'" --portable-root "%~dp0."\r\n'
    for name in ('Start.cmd','Open.cmd','打开档案.cmd'):(target/name).write_bytes(gui.encode('ascii'))
    for name,command,args in [('Restore.cmd','restore',' --destination "%~dp0Recovered" --reuse-immutable'),('Verify.cmd','verify-backup','')]:
        report='Restore-result.private.json' if command=='restore' else 'Verify-result.private.json'
        body='@echo off\r\ncd /d "%~dp0"\r\necho Use Start.cmd for the graphical assistant and safe cancellation.\r\nstart /wait "" "'+exe+'" --report "%~dp0'+report+'" '+command+' --repository "%~dp0."'+args+'\r\necho See '+report+' for the result.\r\npause\r\n'
        (target/name).write_bytes(body.encode('ascii'))

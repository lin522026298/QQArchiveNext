"""Freeze the unified app and bind every delivered source/runtime byte."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024**2):digest.update(block)
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--webview-package',type=Path,default=ROOT/'work/webview2.1.0.3719.77.nupkg')
    args=parser.parse_args();output=args.output.resolve();dist=output/'QQArchiveNext'
    if output.exists():raise SystemExit('use a new release output directory')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
    if dirty:raise SystemExit('commit the reviewed source before building a bound release')
    native=ROOT/'work/qq_native_build/runtime'
    if not (native/'runtime.json').is_file():raise SystemExit('build the private native reader first')
    output.mkdir(parents=True)
    if sha(args.webview_package)!='2f6be3a10a1a8d6d1fde986af4131dab344f8181fffe75590824e0f4b037ed73':raise SystemExit('WebView2 SDK package hash differs')
    sdk=output/'sdk';sdk.mkdir()
    with zipfile.ZipFile(args.webview_package) as package:
        for member in ('lib/net462/Microsoft.Web.WebView2.Core.dll','lib/net462/Microsoft.Web.WebView2.WinForms.dll','runtimes/win-x64/native/WebView2Loader.dll','LICENSE.txt'):
            (sdk/Path(member).name).write_bytes(package.read(member))
    subprocess.run(['powershell','-NoProfile','-File',str(ROOT/'scripts/Build-QqDesktop.ps1'),'-Sdk',str(sdk),'-Output',str(dist)],check=True,cwd=ROOT)
    shutil.copyfile(sdk/'LICENSE.txt',dist/'WebView2-LICENSE.txt')
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--console','--noupx',
      '--name','QQArchiveCore','--paths',str(ROOT),'--paths',str(ROOT/'viewer/qq_nt_archive_viewer/src'),
      '--paths',str(ROOT/'vendor/nt_msg_db_util'),'--collect-submodules','msgdb',
      '--additional-hooks-dir',str(ROOT/'scripts/pyinstaller_hooks'),
      '--hidden-import','pysilk.backends.cython._silk','--hidden-import','pysilk.backends.cffi._silk',
      '--hidden-import','scripts.qq_media_map_v2',
      '--add-data',str(ROOT/'qq_archive_next/product_web')+';qq_archive_next/product_web',
      '--add-binary',str(native/'sqlite3-ntqq-reader.dll')+';qq_archive_next/native',
      '--add-data',str(native/'runtime.json')+';qq_archive_next/native',
      '--exclude-module','PySide6','--exclude-module','shiboken6',
      '--exclude-module','tkinter','--exclude-module','apsw',
      '--exclude-module','pysilk.backends.cffi.build','--exclude-module','setuptools',
      '--distpath',str(dist/'backend'),'--workpath',str(output/'build'),'--specpath',str(output/'spec'),str(ROOT/'scripts/qq_next_launcher.py')]
    # Do not harvest optional DLLs from unrelated Poppler/libheif installations
    # injected into the calling shell's PATH. Keep the compiler process isolated.
    environment=os.environ.copy()
    system=Path(os.environ['SystemRoot'])
    environment['PATH']=os.pathsep.join(map(str,[Path(sys.executable).parent,Path(sys.base_prefix),Path(sys.base_prefix)/'DLLs',system/'System32',system]))
    subprocess.run(command,cwd=ROOT,env=environment,check=True)
    (dist/'使用说明.md').write_text((ROOT/'docs/usage.md').read_text(encoding='utf-8').replace('(agent.md)','(GUI与Agent使用说明.md)'),encoding='utf-8')
    shutil.copyfile(ROOT/'docs/agent.md',dist/'GUI与Agent使用说明.md')
    shutil.copyfile(ROOT/'docs/repository.md',dist/'仓库整理说明.md')
    shutil.copyfile(ROOT/'docs/repository.md',dist/'repository.md')
    shutil.copyfile(ROOT/'scripts/Configure-QQArchive.ps1',dist/'Configure-QQArchive.ps1')
    shutil.copyfile(ROOT/'LICENSE',dist/'LICENSE')
    shutil.copyfile(ROOT/'qq_archive_next/ORIGIN.md',dist/'第三方来源.md')
    source=dist/'source-rebuild';source.mkdir()
    subprocess.run(['git','archive','--format=zip','-o',str(source/'source.zip'),revision],cwd=ROOT,check=True)
    for name in ('sqlite.zip','sqlite3-ntqq.c'):
        shutil.copyfile(ROOT/'work/qq_native_build'/name,source/name)
    shutil.copyfile(native/'runtime.json',source/'runtime.json')
    licenses=dist/'licenses';licenses.mkdir()
    shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt',licenses/'Python-LICENSE.txt')
    shutil.copyfile(ROOT/'vendor/nt_msg_db_util/LICENSE',licenses/'nt-msg-db-util-GPL-3.0.txt')
    packages=['cryptography','protobuf','psutil','silk-python','cffi','pycparser','pyinstaller']
    versions={}
    for name in packages:
        distribution=importlib.metadata.distribution(name);versions[name]=distribution.version
        for relative in distribution.files or ():
            if any('license' in part.casefold() or 'copying' in part.casefold() for part in relative.parts):
                path=distribution.locate_file(relative)
                if path.is_file():
                    target=licenses/name/Path(*relative.parts[1:]);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    metadata={'schema':'qq-next-release/1','git_revision':revision,'product_version':'1.1.1','features':['portable_assistant','quiet_important_notices','unified_agent','rolling_repository','verified_compaction'],
      'python':sys.version,'dependencies':versions,
      'native_runtime':json.loads((native/'runtime.json').read_text(encoding='utf-8')),'files':[]}
    for path in sorted(dist.rglob('*')):
        if path.is_file():metadata['files'].append({'path':path.relative_to(dist).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)})
    (dist/'release_verification.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'release':str(dist),'git_revision':revision,'files':len(metadata['files']),
      'bytes':sum(item['bytes'] for item in metadata['files'])},ensure_ascii=False))


if __name__=='__main__':main()

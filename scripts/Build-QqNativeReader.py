"""Build the private, read-only NTQQ SQLite runtime in a local work directory."""
from __future__ import annotations
import argparse,hashlib,json,subprocess,urllib.request,zipfile
from pathlib import Path

INPUTS=[('zig.zip','https://ziglang.org/download/0.17.0/zig-x86_64-windows-0.17.0.zip','b5663f69581dcf391293fbf16c06cb80d81d806545ce618b4d0bab7f0eb8c428'),
        ('sqlite.zip','https://www.sqlite.org/2026/sqlite-amalgamation-3540100.zip','0a9c8e34352dad46006aa82836d5383bff9dc6bc9ce5a60553c05aa30e6ad1f9')]
FLAGS=['-O2','-shared','-target','x86_64-windows-gnu','-DSQLITE_API=__declspec(dllexport)',
       '-DSQLITE_THREADSAFE=1','-DSQLITE_ENABLE_DBSTAT_VTAB','-DSQLITE_ENABLE_FTS5','-DSQLITE_OMIT_LOAD_EXTENSION',
       '-DSQLITE_TEMP_STORE=2','-DSQLITE_DEFAULT_CACHE_SIZE=-32768','-DSQLITE_MAX_MMAP_SIZE=0']


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root',type=Path,default=Path(__file__).resolve().parents[1]/'work/qq_native_build')
    args=parser.parse_args();root=args.work_root.resolve();root.mkdir(parents=True,exist_ok=True)
    output=root/'runtime';output.mkdir(exist_ok=True)
    manifest=output/'runtime.json';library=output/'sqlite3-ntqq-reader.dll'
    if manifest.is_file() and library.is_file():
        metadata=json.loads(manifest.read_text(encoding='utf-8'))
        if sha(library)==metadata.get('dll_sha256'):
            print(json.dumps({'status':'no_op','runtime':str(output)}));return
        raise SystemExit('Existing runtime changed; use a fresh work root instead of overwriting it.')
    for name,url,expected in INPUTS:
        archive=root/name
        if not archive.exists():
            partial=archive.with_suffix('.download.partial')
            with urllib.request.urlopen(url,timeout=60) as source,partial.open('xb') as target:
                while block:=source.read(1024**2):target.write(block)
            if sha(partial)!=expected:raise SystemExit('Downloaded build input failed SHA-256 verification.')
            partial.replace(archive)
        if sha(archive)!=expected:raise SystemExit('Cached build input failed SHA-256 verification.')
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                if not (root/item.filename).resolve().is_relative_to(root):raise ValueError('unsafe build ZIP path')
            package.extractall(root)
    original=root/'sqlite-amalgamation-3540100/sqlite3.c'
    text=original.read_text(encoding='utf-8')
    anchor='SQLITE_PRIVATE int sqlite3PendingByte = 0x40000000;'
    if text.count(anchor)!=1:raise SystemExit('SQLite patch anchor changed; refusing to build.')
    text=text.replace(anchor,'SQLITE_PRIVATE int sqlite3PendingByte = 0x3ffffc00;')
    text+='\nSQLITE_API int qq_sqlite_pending_byte(void){return sqlite3PendingByte;}\n'
    patched=root/'sqlite3-ntqq.c';patched.write_text(text,encoding='utf-8')
    subprocess.run([str(root/'zig-x86_64-windows-0.17.0/zig.exe'),'cc',*FLAGS,'-o',str(library),str(patched)],check=True)
    metadata={'schema':'qq-native-sqlite-runtime/1','sqlite_version':'3.54.1','compiler':'zig-0.17.0',
              'pending_byte':0x3ffffc00,'dll_sha256':sha(library),'original_source_sha256':sha(original),
              'patched_source_sha256':sha(patched),'inputs':[{'url':u,'sha256':h} for _,u,h in INPUTS]}
    manifest.write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'built','runtime':str(output),'sha256':sha(library)}))


if __name__=='__main__':main()

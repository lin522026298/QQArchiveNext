"""Incremental immutable media objects; message links require byte MD5 + MIME.

Message segment parsing and cache classification follow the existing archive.
Filename/path matches are never promoted to verified content. No CDN requests.
"""
from __future__ import annotations
from collections import defaultdict
import hashlib
import os
import sqlite3
import time
import uuid
import tempfile
import wave
import shutil
from pathlib import Path

from .config import inside,_inside_resolved
from .normalizer import vendor  # binds the already-vendored msgdb parser
from msgdb.proto.c2c_40800_parser import parse_40800
from scripts.qq_media_map_v2 import Asset,decide

COPY_DIRS=('Pic','Ptt','Video','File','Emoji')
SCHEMA='qq-next-media/1'


def detect_mime(header,path):
    if header.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if header.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if header.startswith((b'GIF87a',b'GIF89a')):return 'image/gif'
    if header.startswith(b'RIFF') and header[8:12]==b'WAVE':return 'audio/wav'
    if len(header)>=12 and header[4:8]==b'ftyp':return 'video/mp4'
    if header.startswith(b'ID3') or header.startswith(b'\xff\xfb'):return 'audio/mpeg'
    if header.startswith(b'OggS'):return 'audio/ogg'
    if header.startswith((b'\xff\xf1',b'\xff\xf9')):return 'audio/aac'
    if header.startswith(b'#!SILK_V3') or header[1:10]==b'#!SILK_V3':return 'audio/x-qq-silk'
    return 'application/octet-stream'


def classify(relative):
    parts=[part.casefold() for part in Path(relative).parts];top=parts[0]
    if top=='ptt':return 'voice_silk_or_audio'
    if top=='emoji':return 'emoji_cache'
    if top=='pic':return 'image_original_candidate' if any(p.startswith('ori') for p in parts) else ('image_thumbnail_candidate' if any(p.startswith('thumb') for p in parts) else 'image_cache')
    if top=='video':return 'video_thumbnail' if any(p.startswith('thumb') for p in parts) else ('video_original_candidate' if any(p.startswith('ori') for p in parts) else 'video_cache')
    if top=='file':return 'attachment_preview_candidate' if any(p.startswith('thumb') for p in parts) else 'attachment_cache'
    return 'other_cache'


def segment_kind(content,msg_type):
    ct=int(getattr(content,'content_type',0) or 0)
    identity=bool(content.filename or content.md5_raw or content.video_thumb_md5 or content.local_path)
    if ct==4:return 'voice'
    if msg_type==7 or (ct==9 and identity) or (ct==2 and identity and (content.video_flag or content.f45415)):return 'video'
    if msg_type==3:return 'attachment'
    if ct==2 and (content.img_width or content.img_height or content.md5_raw or content.local_path):return 'image'
    if ct==5 or content.sticker:return 'emoji'
    path=str(content.local_path or '').replace('\\','/').casefold()
    if '/nt_data/' in path:return {'pic':'image','ptt':'voice','video':'video','file':'attachment','emoji':'emoji'}.get(path.split('/nt_data/',1)[1].split('/',1)[0])
    return None


def segments(fields):
    blob=fields.get('40800')
    if not isinstance(blob,bytes):return []
    result=[]
    for index,content in enumerate(parse_40800(blob).contents):
        kind=segment_kind(content,int(fields.get('40011') or 0))
        if kind:result.append({'segment_index':index,'media_type':kind,'primary_md5':bytes(content.md5_raw).hex(),
          'thumbnail_md5':bytes(content.video_thumb_md5).hex(),'filename':str(content.filename or '') or None})
    return result


def connect(root,*,readonly=False):
    path=Path(root)/'assets.sqlite'
    if readonly:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    else:
        existed=path.exists();db=sqlite3.connect(path)
        if existed:
            try:version=db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            except sqlite3.Error:version=None
            if not version or version[0]!=SCHEMA:db.close();raise ValueError('refusing foreign media database')
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS asset(sha256 TEXT PRIMARY KEY,md5 TEXT NOT NULL,byte_count INTEGER NOT NULL,mime TEXT NOT NULL,relative TEXT NOT NULL UNIQUE) WITHOUT ROWID;
          CREATE INDEX IF NOT EXISTS asset_md5 ON asset(md5);
          CREATE TABLE IF NOT EXISTS observation(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,account_key TEXT NOT NULL,source_relative TEXT NOT NULL,
            sha256 TEXT NOT NULL REFERENCES asset(sha256),cache_class TEXT NOT NULL,observed REAL NOT NULL,UNIQUE(source_id,source_relative,sha256));
          CREATE INDEX IF NOT EXISTS observation_account ON observation(account_key,sha256);
          CREATE TABLE IF NOT EXISTS head(source_id TEXT NOT NULL,source_relative TEXT NOT NULL,observation_id INTEGER NOT NULL REFERENCES observation(id),
            signature TEXT NOT NULL,PRIMARY KEY(source_id,source_relative)) WITHOUT ROWID;
          CREATE TRIGGER IF NOT EXISTS asset_no_update BEFORE UPDATE ON asset BEGIN SELECT RAISE(ABORT,'immutable asset');END;
          CREATE TRIGGER IF NOT EXISTS asset_no_delete BEFORE DELETE ON asset BEGIN SELECT RAISE(ABORT,'immutable asset');END;
          CREATE TRIGGER IF NOT EXISTS observation_no_update BEFORE UPDATE ON observation BEGIN SELECT RAISE(ABORT,'immutable observation');END;
          CREATE TRIGGER IF NOT EXISTS observation_no_delete BEFORE DELETE ON observation BEGIN SELECT RAISE(ABORT,'immutable observation');END;''')
        db.execute("INSERT OR IGNORE INTO metadata VALUES('schema',?)",(SCHEMA,));db.commit()
    db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
    if readonly:db.execute('PRAGMA query_only=ON')
    return db


def hash_file(path,cancelled=lambda:False):
    sha=hashlib.sha256();md5=hashlib.md5();count=0
    with Path(path).open('rb') as stream:
        while block:=stream.read(1024**2):
            if cancelled():raise InterruptedError('media cancelled')
            count+=len(block);sha.update(block);md5.update(block)
    return count,sha.hexdigest(),md5.hexdigest()


def capture_media(root,source,*,cancelled=lambda:False,full=False):
    root=Path(root).resolve(strict=True);input_root=Path(source['media_root']).resolve(strict=True)
    db=connect(root);seen=added=copied=unchanged=unavailable=0;started=time.monotonic()
    try:
        for dirname in COPY_DIRS:
            base=input_root/dirname
            if not base.exists():continue
            if base.is_symlink() or not base.resolve().is_relative_to(input_root):raise ValueError('media source symlink/junction')
            for current,dirs,files in os.walk(base,followlinks=False):
                current_path=Path(current)
                relative_directory=current_path.relative_to(input_root)
                dirs[:]=[name for name in dirs if not (current_path/name).is_symlink() and (current_path/name).resolve().is_relative_to(input_root)]
                for name in files:
                    if cancelled():raise InterruptedError('media cancelled')
                    path=current_path/name
                    if path.is_symlink() or not path.resolve().is_relative_to(input_root):continue
                    relative=(relative_directory/name).as_posix();seen+=1
                    try:stat=path.stat()
                    except OSError:unavailable+=1;continue
                    signature=repr((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
                    known=db.execute('SELECT h.signature,a.relative FROM head h JOIN observation o ON o.id=h.observation_id JOIN asset a ON a.sha256=o.sha256 WHERE h.source_id=? AND h.source_relative=?',(source['id'],relative)).fetchone()
                    if not full and known and known['signature']==signature and _inside_resolved(root,known['relative']).is_file():unchanged+=1;continue
                    partial=root/'objects'/('.'+uuid.uuid4().hex+'.partial');partial.parent.mkdir(parents=True,exist_ok=True)
                    try:
                        sha=hashlib.sha256();md5=hashlib.md5();header=b'';size=0
                        with path.open('rb') as input_stream,partial.open('xb') as target:
                            while block:=input_stream.read(1024**2):
                                if cancelled():raise InterruptedError('media cancelled')
                                if not header:header=block[:64]
                                target.write(block);sha.update(block);md5.update(block);size+=len(block)
                            target.flush();os.fsync(target.fileno())
                        after=path.stat()
                        if repr((after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns))!=signature or size!=stat.st_size or hash_file(path,cancelled)[1]!=sha.hexdigest():
                            unavailable+=1;continue
                        digest=sha.hexdigest();object_relative='objects/'+digest[:2]+'/'+digest
                        destination=_inside_resolved(root,object_relative);destination.parent.mkdir(parents=True,exist_ok=True)
                        if destination.exists():
                            if hash_file(destination,cancelled)!=(size,digest,md5.hexdigest()):raise ValueError('existing media object hash mismatch')
                        else:os.replace(partial,destination);copied+=size
                        with db:
                            db.execute('INSERT OR IGNORE INTO asset VALUES(?,?,?,?,?)',(digest,md5.hexdigest(),size,detect_mime(header,path),object_relative))
                            added+=db.execute('INSERT OR IGNORE INTO observation(source_id,account_key,source_relative,sha256,cache_class,observed) VALUES(?,?,?,?,?,?)',
                              (source['id'],source['account_key'],relative,digest,classify(relative),time.time())).rowcount
                            observation=db.execute('SELECT id FROM observation WHERE source_id=? AND source_relative=? AND sha256=?',(source['id'],relative,digest)).fetchone()[0]
                            db.execute('INSERT INTO head VALUES(?,?,?,?) ON CONFLICT(source_id,source_relative) DO UPDATE SET observation_id=excluded.observation_id,signature=excluded.signature',(source['id'],relative,observation,signature))
                    except (FileNotFoundError,PermissionError):unavailable+=1
                    finally:partial.unlink(missing_ok=True)
        return {'files_seen':seen,'observations_added':added,'bytes_copied':copied,'unchanged_hints':unchanged,'unavailable':unavailable,'seconds':round(time.monotonic()-started,3)}
    finally:db.close()


def links(root,account,items):
    """items: (published_id, source_table, raw fields); batch exact MD5 lookups."""
    from qq_nt_archive_viewer.archive_store import MediaAsset,MediaLink
    parsed=[(identity,table,segment) for identity,table,fields in items for segment in segments(fields)]
    if not parsed:return {}
    if not (Path(root)/'assets.sqlite').is_file():db=None
    else:db=connect(root,readonly=True)
    try:
        md5s=sorted({value for _,_,segment in parsed for value in (segment['primary_md5'],segment['thumbnail_md5']) if value})
        assets_by_md5=defaultdict(list);assets_by_id={};asset_rows={}
        if db:
            for offset in range(0,len(md5s),400):
                batch=md5s[offset:offset+400]
                for row in db.execute('''SELECT o.id,o.cache_class,o.source_relative,a.* FROM observation o JOIN asset a ON a.sha256=o.sha256
                  WHERE o.account_key=? AND a.md5 IN ('''+','.join('?' for _ in batch)+')',(account,*batch)):
                    asset=Asset(row['id'],'raw_cache',row['relative'],row['md5'],row['mime'],row['cache_class'],row['source_relative'])
                    assets_by_md5[row['md5']].append(asset);assets_by_id[row['id']]=asset;asset_rows[row['id']]=row
        def view(identity):
            if identity is None:return None
            row=asset_rows[identity];path=inside(root,row['relative'])
            return MediaAsset(row['relative'],row['mime'],row['cache_class'],path if path.is_file() else None,Path(row['source_relative']).name)
        result=defaultdict(list)
        for identity,table,segment in parsed:
            decision=decide(segment,assets_by_md5,assets_by_id)
            primary=view(decision.primary_asset_id);playable=view(decision.playable_asset_id)
            note=decision.note
            if segment['media_type']=='voice' and primary:
                # Conversion belongs to the explicit play action, not pagination.
                playable=primary
            result[identity].append(MediaLink(table,identity,segment['segment_index'],segment['media_type'],decision.role,decision.verification,
              note,segment['filename'],primary,view(decision.preview_asset_id),playable,view(decision.candidate_asset_id)))
        return dict(result)
    finally:
        if db:db.close()


def voice_wav(root,source,*,cancelled=lambda:False):
    import pysilk
    from .materialize import atomic_json
    root=Path(root);source=Path(source)
    digest=hash_file(source,cancelled)[1];target=root/'cache/audio'/(digest+'.wav')
    receipt=target.with_suffix('.json')
    if target.is_file() and receipt.is_file():
        metadata=__import__('json').loads(receipt.read_text(encoding='utf-8'))
        if metadata.get('source_sha256')==digest and metadata.get('wav_sha256')==hash_file(target,cancelled)[1]:return target
    target.parent.mkdir(parents=True,exist_ok=True)
    partial=target.with_name('.'+digest+'.'+uuid.uuid4().hex+'.partial')
    try:
        with source.open('rb') as stream,tempfile.TemporaryFile('w+b') as pcm:
            header=stream.read(10)
            if header[1:10]==b'#!SILK_V3':stream.seek(1)
            elif header[:9]==b'#!SILK_V3':stream.seek(0)
            else:raise ValueError('not a Silk recording')
            class Input:
                def read(self,size=-1):
                    if cancelled():raise InterruptedError('voice preparation cancelled')
                    return stream.read(size)
                def __getattr__(self,name):return getattr(stream,name)
            pysilk.decode(Input(),pcm,24000)
            size=pcm.seek(0,2)
            if size==0 or size%2:raise ValueError('invalid decoded PCM')
            pcm.seek(0)
            with wave.open(str(partial),'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000)
                while block:=pcm.read(1024**2):
                    if cancelled():raise InterruptedError('voice preparation cancelled')
                    output.writeframesraw(block)
        os.replace(partial,target)
        atomic_json(receipt,{'source_sha256':digest,'decoder':'silk-python/0.2.8','wav_sha256':hash_file(target)[1]})
        return target
    finally:partial.unlink(missing_ok=True)


def external_copy(source,filename=None):
    """Make an editable alias only when explicitly opening an object externally."""
    import re
    source=Path(source)
    cas=source.parent.parent.name=='objects' or (source.parent.parent.name=='sha256' and source.parent.parent.parent.name=='objects')
    if not cas or not re.fullmatch('[0-9a-f]{64}',source.name):return source
    name=Path(filename or source.name).name
    name=''.join(c if c.isalnum() or c in '._- ' else '_' for c in name)[:120].strip(' .') or 'file'
    root=next((p for p in source.parents if (p/'OWNER.json').is_file() and (p/'config.private.json').is_file()),None)
    if root is None:root=Path(os.environ.get('LOCALAPPDATA') or __import__('tempfile').gettempdir())/'QQArchiveNext'
    destination=root/'cache/open'/source.name/(source.name[:12]+'_'+name)
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        if hash_file(destination)[1]==source.name:return destination
        destination=destination.with_name(destination.stem+'.'+uuid.uuid4().hex+destination.suffix)
    temporary=destination.with_name('.'+uuid.uuid4().hex+'.partial')
    try:
        shutil.copyfile(source,temporary)
        if hash_file(temporary)[1]!=source.name:raise ValueError('media object changed before external open')
        os.replace(temporary,destination)
        return destination
    finally:temporary.unlink(missing_ok=True)


def verify(root,*,cancelled=lambda:False):
    db=connect(root,readonly=True);count=size=0
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(db.execute('PRAGMA foreign_key_check')):raise ValueError('media catalog integrity failure')
        for row in db.execute('SELECT * FROM asset'):
            actual=hash_file(inside(root,row['relative']),cancelled)
            if actual!=(row['byte_count'],row['sha256'],row['md5']):raise ValueError('media object hash mismatch')
            count+=1;size+=row['byte_count']
        return {'objects_verified':count,'bytes_verified':size}
    finally:db.close()

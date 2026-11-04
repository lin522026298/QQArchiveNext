"""Exercise the actual frozen EXE with synthetic native QQ input and restore."""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
import math
import os
import sqlite3
import struct
import subprocess
import sys
import uuid
import zlib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from qq_next_fixture import native,wrap,PASSWORD
from qq_archive_next.materialize import atomic_json
from qq_archive_next.backup import read_point
from qq_archive_next import media
from msgdb.proto.c2c_40800_pb2 import MsgBody


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release',type=Path,required=True);parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args();release=args.release.resolve(strict=True);work=args.work.resolve()
    if work.exists():raise SystemExit('verification needs a new owned work directory')
    work.mkdir(parents=True);source=work/'source';source.mkdir();state=work/'state';state.mkdir()
    manifest=json.loads((release/'release_verification.json').read_text(encoding='utf-8'))
    expected={item['path'] for item in manifest['files']}
    actual={p.relative_to(release).as_posix() for p in release.rglob('*') if p.is_file() and p.name!='release_verification.json'}
    if expected!=actual:raise ValueError('release file set changed')
    for item in manifest['files']:
        path=release/item['path']
        if path.stat().st_size!=item['bytes'] or media.hash_file(path)[1]!=item['sha256']:raise ValueError('release bytes differ')
    media_root=source/'nt_data';(media_root/'Pic').mkdir(parents=True);(media_root/'Ptt').mkdir()
    def png_chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    picture=b'\x89PNG\r\n\x1a\n'+png_chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+png_chunk(b'IDAT',zlib.compress(b'\0\x20\x80\xc0'))+png_chunk(b'IEND',b'')
    (media_root/'Pic/synthetic.png').write_bytes(picture)
    import pysilk
    pcm=b''.join(struct.pack('<h',int(500*math.sin(2*math.pi*440*n/24000))) for n in range(4800))
    encoded=io.BytesIO();pysilk.encode(io.BytesIO(pcm),encoded,24000,24000);voice=encoded.getvalue()
    (media_root/'Ptt/synthetic.silk').write_bytes(voice)
    plain=source/'plain.sqlite';native(plain);db=sqlite3.connect(plain)
    db.execute('''CREATE TABLE c2c_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,"40013" INTEGER,"40020" TEXT,"40033" INTEGER,
      "40021" TEXT,"40030" INTEGER,"40011" INTEGER,"40012" INTEGER,"40800" BLOB,"40090" TEXT,"40093" TEXT)''')
    for identity in range(-5,5):
        body=MsgBody();segment=body.content.add();segment.content_type=1;segment.text='合成归档测试 '+str(identity)
        db.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identity,100+identity,1,'test-sender',12345,'test-peer',45678,2,0,body.SerializeToString(),None,None))
    for identity,kind,ct,payload,name in [(10,2,2,picture,'synthetic.png'),(11,6,4,voice,'synthetic.silk')]:
        body=MsgBody();segment=body.content.add();segment.content_type=ct;segment.md5_raw=hashlib.md5(payload).digest();segment.filename=name
        if ct==2:segment.img_width=1;segment.img_height=1
        db.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identity,100+identity,1,'test-sender',12345,'test-peer',45678,kind,0,body.SerializeToString(),None,None))
    db.commit();db.close();wrap(plain,source/'nt_msg.db')
    # Existing standard shm exercises the frozen Windows shared-lock path too.
    if os.name=='nt':(source/'nt_msg.db-shm').write_bytes(bytes(32768))
    state_id=str(uuid.uuid4());atomic_json(state/'OWNER.json',{'schema':'qq-next-state/1','id':state_id})
    atomic_json(state/'config.private.json',{'schema':'qq-next-config/1','state_id':state_id,'auto_capture':False,
      'sources':[{'id':'synthetic','account_key':'synthetic-account','account_label':'12345','database':str(source/'nt_msg.db'),
                  'key_file':'private/source.key','media_root':str(media_root)}]})
    (state/'private').mkdir();(state/'private/source.key').write_bytes(PASSWORD)
    executable=release/'QQArchiveNext.exe'
    environment=os.environ.copy()
    if os.name=='nt':environment['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    def command(name,arguments,*,current_state=state):
        report=work/(name+'.private.json')
        completed=subprocess.run([str(executable),'--state',str(current_state),'--report',str(report),*arguments],timeout=180,
          env=environment,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if completed.returncode or not report.exists():raise ValueError('frozen command failed: '+name)
        return json.loads(report.read_text(encoding='utf-8'))
    capture=command('capture',['capture','--full'])
    if capture['sources']['synthetic']['import']['message_versions_added']!=12:raise ValueError('frozen capture count mismatch')
    if os.name=='nt':
        if capture['sources']['synthetic']['capture']['source_read_guard']!='win32_sqlite_read0':raise ValueError('frozen readonly checkpoint guard missing')
        if (source/'nt_msg.db-shm').read_bytes()!=bytes(32768):raise ValueError('checkpoint guard changed source shm bytes')
    idle=command('idle',['capture'])
    if not idle['sources']['synthetic']['import']['no_op']:raise ValueError('frozen idle capture grew')
    repo=work/'repository';first=command('backup',['backup','--repository',str(repo)])
    second=command('backup-noop',['backup','--repository',str(repo)])
    if first['id']!=second['id'] or second['new_object_bytes']!=0:raise ValueError('frozen backup not idempotent')
    restored=work/'restored';receipt=command('restore',['restore','--repository',str(repo),'--destination',str(restored)])
    if not receipt['deep_semantic_verification'] or receipt['verification']['logical_messages']!=12:raise ValueError('frozen restore mismatch')
    # A reading session through the same API checks replay and media associations.
    from qq_archive_next.reader import NextArchiveStore
    reader=NextArchiveStore(restored/'viewer-synthetic-account.json')
    try:
        conversations=reader.conversations();messages=reader.latest_messages(conversations[0]);links=reader.media_for_messages('c2c_messages',messages)
        verified=[link for values in links.values() for link in values if link.verification=='verified_content']
        if len(verified)!=2 or not any(link.playable for link in verified if link.media_type=='voice'):raise ValueError('restored media association/playback derivative failed')
        voice_link=next(link for link in verified if link.media_type=='voice')
        media.voice_wav(restored,voice_link.primary.path)
        if len(reader.search('合成',limit=20))!=10:raise ValueError('restored literal search mismatch')
    finally:reader.close()
    result={'status':'ok','release_revision':manifest['git_revision'],'files_verified':len(manifest['files']),
      'frozen_capture_messages':12,'restored_messages':12,'verified_media_links':2,'idle_new_backup_bytes':0,
      'state':str(state),'restored':str(restored)}
    atomic_json(work/'verification.private.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()

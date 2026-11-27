"""Page-granular backup blocks in one SQLite pack, with authenticated trees.

SQLite files use 4 KiB blocks; large media use 4 MiB. Tree nodes and file-map
buckets are shared too, so a changed page does not rewrite a huge manifest.
Existing v1 containers can back immutable slices without copying their bytes.
"""
from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
import sqlite3
import struct
import zlib
from pathlib import Path

DATA,LIST,FILES,INDEX=0,1,2,3
FANOUT=64
REFERENCE=struct.Struct('<QBQQ32s')
MAGIC=b'QQBT1'
MAX_BLOCK=4*1024**2
SCHEMA='qq-next-backup-blocks/1'


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def ref(row,span=None):
    return {'id':row['id'],'kind':row['kind'],'bytes':row['byte_count'],
      'span':row['byte_count'] if span is None else span,'sha256':bytes(row['digest']).hex()}


class Pack:
    def __init__(self,repo,*,readonly=False,cancelled=lambda:False,legacy_repo=None,bulk_build=False):
        self.repo=Path(repo);self.path=self.repo/'blocks.sqlite';self.readonly=readonly;self.cancelled=cancelled
        self.legacy_repo=Path(legacy_repo) if legacy_repo else self.repo
        existed=self.path.exists();self.new_bytes=self.added=self.pending=0
        if bulk_build and (readonly or existed or not (self.repo/'INCOMPLETE.json').is_file()):
            raise ValueError('bulk construction requires a fresh incomplete generation')
        self.db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30) if readonly else sqlite3.connect(self.path,timeout=30)
        self.db.row_factory=sqlite3.Row
        if existed or readonly:
            try:schema=self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            except sqlite3.Error:schema=None
            if not schema or schema[0]!=SCHEMA:self.db.close();raise ValueError('not an owned backup block pack')
        if not readonly:
            self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA cache_size=-65536;')
            unique='' if bulk_build else ' UNIQUE'
            self.db.executescript(f'''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
              CREATE TABLE IF NOT EXISTS block(id INTEGER PRIMARY KEY,digest BLOB NOT NULL{unique},kind INTEGER NOT NULL,
                byte_count INTEGER NOT NULL,codec INTEGER NOT NULL,data BLOB NOT NULL);
              CREATE TRIGGER IF NOT EXISTS block_no_update BEFORE UPDATE ON block BEGIN SELECT RAISE(ABORT,'immutable backup block');END;
              CREATE TRIGGER IF NOT EXISTS block_no_delete BEFORE DELETE ON block BEGIN SELECT RAISE(ABORT,'immutable backup block');END;''')
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema',?)",(SCHEMA,));self.db.commit()
            if bulk_build:
                # Disposable until the final flush and INCOMPLETE removal. Build
                # the digest index once after sequential row-ID insertion.
                self.db.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;')
        else:self.db.execute('PRAGMA query_only=ON')

    def close(self):
        self._legacy.cache_clear()
        self.db.close()
    def commit(self):self.db.commit();self.pending=0

    @lru_cache(maxsize=4)
    def _legacy(self,digest,size):
        from .backup import read_chunk
        return read_chunk(self.legacy_repo,{'sha256':digest,'bytes':size})

    def decode(self,row):
        payload=bytes(row['data']);size=row['byte_count']
        if row['codec']==0:result=payload
        elif row['codec']==1:
            decoder=zlib.decompressobj();result=decoder.decompress(payload,size+1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('invalid compressed backup block')
        elif row['codec']==2:
            if row['kind']!=DATA or len(payload)!=52:raise ValueError('invalid legacy block slice')
            digest,container_size,offset,length=struct.unpack('<32sQQI',payload)
            if container_size>MAX_BLOCK or length!=size or offset+length>container_size:raise ValueError('legacy slice escapes container')
            result=self._legacy(digest.hex(),container_size)[offset:offset+length]
        else:raise ValueError('unknown backup block codec')
        if len(result)!=size or hashlib.sha256(bytes([row['kind']])+result).digest()!=row['digest']:raise ValueError('backup block hash mismatch')
        return result

    def get(self,reference):
        if self.cancelled():raise InterruptedError('backup block operation cancelled')
        if not isinstance(reference,dict) or not isinstance(reference.get('id'),int) or reference.get('kind') not in (DATA,LIST,FILES,INDEX):raise ValueError('invalid backup tree reference')
        row=self.db.execute('SELECT * FROM block WHERE id=?',(reference['id'],)).fetchone()
        if row is None or row['kind']!=reference['kind'] or row['byte_count']!=reference['bytes'] or bytes(row['digest']).hex()!=reference['sha256']:
            raise ValueError('backup tree reference differs from block')
        return self.decode(row)

    def put(self,data,kind=DATA,*,span=None,legacy=None):
        if self.readonly:raise ValueError('read-only block pack')
        if self.cancelled():raise InterruptedError('backup block operation cancelled')
        digest=hashlib.sha256(bytes([kind])+data).digest()
        old=self.db.execute('SELECT * FROM block WHERE digest=?',(digest,)).fetchone()
        if old:
            if old['kind']!=kind or self.decode(old)!=data:raise ValueError('backup digest collision')
            return ref(old,span)
        if legacy is not None:
            if kind!=DATA:raise ValueError('only data may reference legacy slices')
            container,offset=legacy
            encoded=struct.pack('<32sQQI',bytes.fromhex(container['sha256']),container['bytes'],offset,len(data));codec=2
        else:
            compressed=zlib.compress(data,3)
            encoded,codec=(compressed,1) if len(compressed)+16<len(data) else (data,0)
        identity=self.db.execute('INSERT INTO block(digest,kind,byte_count,codec,data) VALUES(?,?,?,?,?)',(digest,kind,len(data),codec,encoded)).lastrowid
        self.new_bytes+=len(encoded);self.added+=1;self.pending+=1
        if self.pending>=4096:self.commit()
        return {'id':identity,'kind':kind,'bytes':len(data),'span':len(data) if span is None else span,'sha256':digest.hex()}

    def node(self,children):
        payload=MAGIC+b''.join(REFERENCE.pack(item['id'],item['kind'],item['bytes'],item['span'],bytes.fromhex(item['sha256'])) for item in children)
        return self.put(payload,LIST,span=sum(item['span'] for item in children))

    def file(self,path,*,block_size,previous=None):
        digest=hashlib.sha256();size=0;levels=[]
        # A prior point authenticates its reference tree. Compare fresh input
        # hashes to those references; unchanged pages need neither a database
        # lookup nor re-decompression of an old 4 MiB container. Full repository
        # payload verification remains an explicit verify/restore operation.
        prior=self.data_refs(previous['root']) if previous else iter(())
        def page(data):
            old=next(prior,None)
            if old and old['bytes']==len(data) and old['span']==len(data) and old['sha256']==hashlib.sha256(bytes([DATA])+data).hexdigest():return old
            return self.put(data)
        def push(item,level=0):
            while len(levels)<=level:levels.append([])
            levels[level].append(item)
            if len(levels[level])==FANOUT:
                parent=self.node(levels[level]);levels[level]=[];push(parent,level+1)
        with Path(path).open('rb') as stream:
            while data:=stream.read(block_size):
                digest.update(data);size+=len(data);push(page(data))
        if not size:push(page(b''))
        while True:
            occupied=[n for n,items in enumerate(levels) if items]
            lowest=occupied[0]
            if len(occupied)==1 and len(levels[lowest])==1:
                root=levels[lowest][0];break
            parent=self.node(levels[lowest]);levels[lowest]=[];push(parent,lowest+1)
        return {'bytes':size,'sha256':digest.hexdigest(),'root':root}

    def data_refs(self,reference,ancestors=frozenset()):
        """Authenticate tree nodes; reuse data refs checked against fresh input."""
        if self.cancelled():raise InterruptedError('backup cancelled')
        identity=reference['id']
        if identity in ancestors:raise ValueError('cyclic backup tree')
        if reference['kind']==DATA:
            if reference['span']!=reference['bytes']:raise ValueError('data span mismatch')
            yield reference;return
        payload=self.get(reference)
        if reference['kind']!=LIST or not payload.startswith(MAGIC) or (len(payload)-len(MAGIC))%REFERENCE.size:raise ValueError('invalid backup block list')
        children=[]
        for offset in range(len(MAGIC),len(payload),REFERENCE.size):
            block,kind,length,span,digest=REFERENCE.unpack_from(payload,offset)
            children.append({'id':block,'kind':kind,'bytes':length,'span':span,'sha256':digest.hex()})
        if not 1<=len(children)<=FANOUT or sum(child['span'] for child in children)!=reference['span']:raise ValueError('invalid backup tree span')
        for child in children:yield from self.data_refs(child,ancestors|{identity})

    def data(self,reference,ancestors=frozenset()):
        identity=reference['id']
        if identity in ancestors:raise ValueError('cyclic backup tree')
        payload=self.get(reference)
        if reference['kind']==DATA:
            if reference['span']!=len(payload):raise ValueError('data span mismatch')
            yield payload;return
        if reference['kind']!=LIST or not payload.startswith(MAGIC) or (len(payload)-len(MAGIC))%REFERENCE.size:raise ValueError('invalid backup block list')
        children=[]
        for offset in range(len(MAGIC),len(payload),REFERENCE.size):
            block,kind,length,span,digest=REFERENCE.unpack_from(payload,offset)
            children.append({'id':block,'kind':kind,'bytes':length,'span':span,'sha256':digest.hex()})
        if not 1<=len(children)<=FANOUT or sum(child['span'] for child in children)!=reference['span']:raise ValueError('invalid backup tree span')
        for child in children:yield from self.data(child,ancestors|{identity})

    def file_tree(self,entries,previous=None):
        buckets=defaultdict(dict)
        for name,entry in entries.items():
            prefix=hashlib.sha256(name.encode()).hexdigest()[:4];buckets[prefix][name]=entry
        old_root=json.loads(self.get(previous['root'])) if previous else {}
        old_branches={prefix:json.loads(self.get(branch)) for prefix,branch in old_root.items()}
        def shared(data,kind,old):
            if self.cancelled():raise InterruptedError('backup cancelled')
            if old and old['kind']==kind and old['bytes']==len(data) and old['span']==0 and old['sha256']==hashlib.sha256(bytes([kind])+data).hexdigest():return old
            return self.put(data,kind,span=0)
        branches=defaultdict(dict)
        for prefix,values in sorted(buckets.items()):
            branches[prefix[:2]][prefix[2:]]=shared(canonical(values),FILES,old_branches.get(prefix[:2],{}).get(prefix[2:]))
        root={prefix:shared(canonical(values),INDEX,old_root.get(prefix)) for prefix,values in sorted(branches.items())}
        return {'schema':'qq-next-file-tree/1','root':shared(canonical(root),INDEX,previous['root'] if previous else None),'count':len(entries),
          'logical_bytes':sum(entry['bytes'] for entry in entries.values())}

    def files(self,tree):
        if tree.get('schema')!='qq-next-file-tree/1':raise ValueError('invalid backup file tree')
        root=json.loads(self.get(tree['root']));count=total=0
        if len(root)>256:raise ValueError('invalid file tree width')
        for prefix,branch in sorted(root.items()):
            if len(prefix)!=2 or any(c not in '0123456789abcdef' for c in prefix) or branch['kind']!=INDEX:raise ValueError('invalid file prefix')
            children=json.loads(self.get(branch))
            if len(children)>256:raise ValueError('invalid file bucket width')
            for suffix,leaf in sorted(children.items()):
                if len(suffix)!=2 or any(c not in '0123456789abcdef' for c in suffix) or leaf['kind']!=FILES:raise ValueError('invalid file bucket')
                for name,entry in sorted(json.loads(self.get(leaf)).items()):
                    if hashlib.sha256(name.encode()).hexdigest()[:4]!=prefix+suffix:raise ValueError('file in wrong hash bucket')
                    count+=1;total+=entry['bytes'];yield name,entry
        if count!=tree['count'] or total!=tree['logical_bytes']:raise ValueError('backup file tree totals differ')

    def seed_legacy(self,point,*,progress=None):
        seeded=self.db.execute("SELECT value FROM metadata WHERE key='legacy_seed'").fetchone()
        if seeded:return 0
        before=self.added
        if point is not None:
            for name,entry in sorted(point['body']['files'].items()):
                step=4096 if name.endswith('.sqlite') else MAX_BLOCK
                for container in entry['chunks']:
                    data=self._legacy(container['sha256'],container['bytes'])
                    for offset in range(0,len(data),step):
                        self.put(data[offset:offset+step],legacy=(container,offset))
                        if progress and self.added%100000==0:progress(self.added)
        self.db.execute("INSERT INTO metadata VALUES('legacy_seed',?)",(point['id'] if point else 'none',));self.commit()
        return self.added-before

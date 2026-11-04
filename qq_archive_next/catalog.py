"""One transactional archive: exact raw versions, shared content, current view.

All native rows are scanned, including old IDs and unknown tables. Missing rows
are never interpreted as deletion. No committed scan is published in part.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import codec
from .content_pool import ContentPool, sql_text_expression
from .normalizer import VERSION, normalize, fingerprint

SCHEMA='qq-next-catalog/1'


def quote(name):return '"'+name.replace('"','""')+'"'
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


class Catalog:
    def __init__(self,path:Path,*,readonly=False):
        self.path=Path(path).resolve()
        self.readonly=readonly
        if self.path.exists() and not readonly:
            probe=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
            try:
                try:version=probe.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                except sqlite3.Error:version=None
                if not version or version[0]!=SCHEMA:raise ValueError('refusing to adopt a non-owned database')
            finally:probe.close()
        if readonly:
            self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=30)
        else:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(self.path.resolve().as_uri(),uri=True,timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        if not readonly:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA cache_size=-65536')
            self.db.execute('PRAGMA temp_store=MEMORY')
        existing=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone()
        if not existing:
            if readonly:raise ValueError('catalog is not initialized')
            self._create()
        schema=self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not schema or schema[0]!=SCHEMA:raise ValueError('unsupported QQ catalog schema')
        self.pool=ContentPool(self.db)
        self._column_cache={}
        if not readonly:self._ensure_projection()
        if readonly:self.db.execute('PRAGMA query_only=ON')

    def _ensure_projection(self):
        body_ready=self.db.execute("SELECT value FROM metadata WHERE key='body_view_version'").fetchone()
        if not body_ready:
            expression=sql_text_expression('p')
            self.db.execute('DROP VIEW body_text')
            self.db.execute(f'''CREATE VIEW body_text AS SELECT b.id,CASE WHEN p.kind='text' THEN {expression} END AS text
              FROM body_content b JOIN content_payload p ON p.id=b.id''')
            self.db.execute("INSERT INTO metadata VALUES('body_view_version','1')")
            self.db.commit()
        self.db.executescript('''CREATE TABLE IF NOT EXISTS current_message(
          id INTEGER PRIMARY KEY,account_key TEXT NOT NULL,kind TEXT NOT NULL,version_id INTEGER NOT NULL REFERENCES message_version(version_id),
          timestamp INTEGER,direction INTEGER,sender_uid TEXT,sender_qq INTEGER,chat_uid TEXT,chat_qq INTEGER,msg_type INTEGER,
          body_id INTEGER NOT NULL REFERENCES content_payload(id),normalized_id INTEGER NOT NULL REFERENCES content_payload(id),
          historic_label TEXT,historic_label_source TEXT);
          CREATE INDEX IF NOT EXISTS current_qq ON current_message(account_key,kind,chat_qq,chat_uid,timestamp,id);
          CREATE INDEX IF NOT EXISTS current_uid ON current_message(account_key,kind,chat_uid,timestamp,id);
          CREATE INDEX IF NOT EXISTS current_body ON current_message(account_key,body_id,id);
          CREATE TABLE IF NOT EXISTS conversation_summary(account_key TEXT NOT NULL,kind TEXT NOT NULL,
            chat_qq INTEGER NOT NULL,chat_uid TEXT NOT NULL,message_count INTEGER NOT NULL,
            first_timestamp INTEGER,last_timestamp INTEGER,PRIMARY KEY(account_key,kind,chat_qq,chat_uid)) WITHOUT ROWID;''')
        ready=self.db.execute("SELECT value FROM metadata WHERE key='projection_version'").fetchone()
        if not ready:
            with self.db:
                self._sync_projection()
                self.db.execute("INSERT INTO metadata VALUES('projection_version','1')")
        summaries=self.db.execute("SELECT value FROM metadata WHERE key='conversation_summary_version'").fetchone()
        if not summaries:
            with self.db:
                self._sync_summaries()
                self.db.execute("INSERT INTO metadata VALUES('conversation_summary_version','1')")

    def _sync_summaries(self,*,account=None,dirty=False):
        columns='account_key,kind,chat_qq,chat_uid'
        condition=' WHERE ('+columns+') IN (SELECT '+columns+' FROM next_dirty_conversation)' if dirty else (' WHERE account_key=?' if account else '')
        parameters=(account,) if account else ()
        self.db.execute('DELETE FROM conversation_summary'+condition,parameters)
        self.db.execute('INSERT INTO conversation_summary SELECT '+columns+',count(*),min(timestamp),max(timestamp) FROM current_message'+condition+' GROUP BY '+columns,parameters)

    def _sync_projection(self,*,dirty=False,account=None):
        if dirty:
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS next_dirty_conversation(account_key TEXT,kind TEXT,chat_qq INTEGER,chat_uid TEXT,PRIMARY KEY(account_key,kind,chat_qq,chat_uid)) WITHOUT ROWID')
            self.db.execute('DELETE FROM next_dirty_conversation')
            self.db.execute('''INSERT OR IGNORE INTO next_dirty_conversation SELECT account_key,kind,chat_qq,chat_uid
              FROM current_message WHERE id IN (SELECT id FROM next_dirty_published)''')
        condition='WHERE h.published_id IN (SELECT id FROM next_dirty_published)' if dirty else ('WHERE h.account_key=?' if account else '')
        parameters=(account,) if account else ()
        self.db.execute('DELETE FROM current_message'+(' WHERE id IN (SELECT id FROM next_dirty_published)' if dirty else (' WHERE account_key=?' if account else '')),parameters)
        self.db.execute('''INSERT INTO current_message SELECT h.published_id,h.account_key,v.kind,v.version_id,
          v.timestamp,v.direction,v.sender_uid,nullif(CAST(v.sender_qq AS INTEGER),0),v.chat_uid,CAST(v.chat_qq AS INTEGER),v.msg_type,
          v.body_id,v.normalized_id,v.historic_label,v.historic_label_source
          FROM message_head h JOIN message_version v ON v.version_id=h.version_id '''+
          (condition+' AND h.representative=1' if condition else 'WHERE h.representative=1'),parameters)
        if dirty:
            self.db.execute('''INSERT OR IGNORE INTO next_dirty_conversation SELECT account_key,kind,chat_qq,chat_uid
              FROM current_message WHERE id IN (SELECT id FROM next_dirty_published)''')
        self._sync_summaries(account=account,dirty=dirty)

    def _create(self):
        self.db.executescript('''
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE source(id TEXT PRIMARY KEY,account_key TEXT NOT NULL,kind TEXT NOT NULL,
          binding TEXT NOT NULL,parser_version TEXT NOT NULL,latest_capture INTEGER);
        CREATE TABLE source_table(source_id TEXT NOT NULL REFERENCES source(id),name TEXT NOT NULL,
          schema_json TEXT NOT NULL,PRIMARY KEY(source_id,name)) WITHOUT ROWID;
        CREATE TABLE capture(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES source(id),
          cut_json TEXT NOT NULL,finished REAL NOT NULL);
        CREATE TABLE capture_totals(capture_id INTEGER PRIMARY KEY REFERENCES capture(id),rows_scanned INTEGER NOT NULL,versions_added INTEGER NOT NULL,heads_changed INTEGER NOT NULL);
        CREATE TABLE raw_version(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES source(id),
          source_table TEXT NOT NULL,row_key BLOB NOT NULL,row_hash BLOB NOT NULL,payload_id INTEGER NOT NULL REFERENCES content_payload(id),
          capture_id INTEGER NOT NULL REFERENCES capture(id),UNIQUE(source_id,source_table,row_key,row_hash));
        CREATE TABLE row_head(source_id TEXT NOT NULL,source_table TEXT NOT NULL,row_key BLOB NOT NULL,
          version_id INTEGER NOT NULL REFERENCES raw_version(id),row_hash BLOB NOT NULL,
          PRIMARY KEY(source_id,source_table,row_key)) WITHOUT ROWID;
        CREATE TABLE message_version(version_id INTEGER PRIMARY KEY REFERENCES raw_version(id),kind TEXT NOT NULL,
          normalized_id INTEGER NOT NULL REFERENCES content_payload(id),body_id INTEGER NOT NULL REFERENCES content_payload(id),
          timestamp INTEGER,chat_qq TEXT,chat_uid TEXT,sender_qq TEXT,sender_uid TEXT,
          direction INTEGER,msg_type INTEGER,historic_label TEXT,historic_label_source TEXT,source_msg_id TEXT NOT NULL);
        CREATE TABLE message_head(source_id TEXT NOT NULL REFERENCES source(id),source_table TEXT NOT NULL,row_key BLOB NOT NULL,
          account_key TEXT NOT NULL,version_id INTEGER NOT NULL REFERENCES message_version(version_id),fingerprint BLOB NOT NULL,
          logical_id TEXT NOT NULL,published_id INTEGER NOT NULL,representative INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(source_id,source_table,row_key)) WITHOUT ROWID;
        CREATE INDEX heads_fingerprint ON message_head(account_key,fingerprint);
        CREATE INDEX heads_logical ON message_head(logical_id);
        CREATE INDEX heads_published ON message_head(published_id,representative);
        CREATE INDEX versions_conversation ON message_version(kind,chat_qq,chat_uid,timestamp,version_id);
        CREATE TABLE fold_history(capture_id INTEGER NOT NULL REFERENCES capture(id),version_id INTEGER NOT NULL REFERENCES raw_version(id),
          previous_logical_id TEXT NOT NULL,logical_id TEXT NOT NULL,rule TEXT NOT NULL);
        ''')
        ContentPool(self.db,create=True)
        self.db.executescript('''CREATE TABLE body_content(id INTEGER PRIMARY KEY REFERENCES content_payload(id));''')
        expression=sql_text_expression('p')
        self.db.execute(f'''CREATE VIEW body_text AS SELECT b.id,CASE WHEN p.kind='text' THEN {expression} END AS text FROM body_content b JOIN content_payload p ON p.id=b.id''')
        self.db.execute("CREATE VIRTUAL TABLE body_fts USING fts5(text,content='body_text',content_rowid='id',tokenize='trigram',detail=none)")
        self.db.execute("INSERT INTO metadata VALUES('schema',?)",(SCHEMA,))
        for table in ('raw_version','message_version','capture','capture_totals','source_table'):
            for operation in ('UPDATE','DELETE'):
                self.db.execute(f'''CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table}
                  BEGIN SELECT RAISE(ABORT,'archive evidence is immutable');END''')
        self.db.commit()

    def close(self):self.db.close()

    def raw_values(self,payload_id):
        kind=self.db.execute('SELECT kind FROM content_payload WHERE id=?',(payload_id,)).fetchone()[0]
        if kind=='bytes':return codec.decode_binary(self.pool.get_bytes(payload_id))
        return codec.decode(self.pool.get_text(payload_id))

    def normalized(self,version_id):
        row=self.db.execute('''SELECT v.normalized_id,r.payload_id,r.source_id,r.source_table FROM message_version v
          JOIN raw_version r ON r.id=v.version_id WHERE v.version_id=?''',(version_id,)).fetchone()
        value=json.loads(self.pool.get_text(row['normalized_id']))
        if value.get('schema')!='qq-normalization-replay/1':return value
        if value.get('parser_version')!=VERSION:raise ValueError('normalization replay version mismatch')
        key=(row['source_id'],row['source_table'])
        if key not in self._column_cache:
            schema=self.db.execute('SELECT schema_json FROM source_table WHERE source_id=? AND name=?',key).fetchone()[0]
            self._column_cache[key]=json.loads(schema)['columns']
        fields=dict(zip(self._column_cache[key],self.raw_values(row['payload_id'])))
        result=normalize(row['source_table'],fields)
        if result is None:raise ValueError('normalization replay is unavailable')
        normalized=result[1]
        if hashlib.sha256(canonical(normalized).encode()).hexdigest()!=value['sha256']:
            raise ValueError('normalization replay differs from observed interpretation')
        return normalized

    def native_fields(self,version_id):
        row=self.db.execute('SELECT source_id,source_table,payload_id FROM raw_version WHERE id=?',(version_id,)).fetchone()
        if row is None:raise ValueError('unknown observation')
        key=(row['source_id'],row['source_table'])
        if key not in self._column_cache:
            self._column_cache[key]=json.loads(self.db.execute('SELECT schema_json FROM source_table WHERE source_id=? AND name=?',key).fetchone()[0])['columns']
        return dict(zip(self._column_cache[key],self.raw_values(row['payload_id'])))

    def _body(self,text):
        identity=self.pool.intern_text(text)
        result=self.db.execute('INSERT OR IGNORE INTO body_content VALUES(?)',(identity,))
        if result.rowcount:
            self.db.execute('INSERT INTO body_fts(rowid,text) VALUES(?,?)',(identity,text))
        return identity

    @staticmethod
    def _published_id(logical):
        return int.from_bytes(hashlib.sha256(logical.encode()).digest()[:8],'big') & 0x7fffffffffffffff

    def _regroup(self,account,fingerprints,capture):
        for digest in fingerprints:
            rows=list(self.db.execute('''SELECT h.*,s.kind FROM message_head h JOIN source s ON s.id=h.source_id
                WHERE h.account_key=? AND h.fingerprint=? ORDER BY s.kind,h.source_id,h.source_table,h.row_key''',(account,digest)))
            fold=len(rows)>1 and len({r['source_id'] for r in rows})==len(rows)
            shared='strict_'+hashlib.sha256(account.encode()+digest).hexdigest()
            for ordinal,row in enumerate(rows):
                logical=shared if fold else 'obs_'+hashlib.sha256(row['source_id'].encode()+row['source_table'].encode()+row['row_key']).hexdigest()
                published=self._published_id(logical)
                collision=self.db.execute('SELECT logical_id FROM message_head WHERE published_id=? AND logical_id!=? LIMIT 1',(published,logical)).fetchone()
                if collision:raise ValueError('published message ID collision')
                if logical!=row['logical_id']:
                    self.db.execute('INSERT INTO fold_history VALUES(?,?,?,?,?)',(capture,row['version_id'],row['logical_id'],logical,'strict_one_to_one_cross_source' if fold else 'separate_observation'))
                self.db.executemany('INSERT OR IGNORE INTO next_dirty_published VALUES(?)',[(row['published_id'],),(published,)])
                self.db.execute('''UPDATE message_head SET logical_id=?,published_id=?,representative=?
                  WHERE source_id=? AND source_table=? AND row_key=?''',
                  (logical,published,int(not fold or ordinal==0),row['source_id'],row['source_table'],row['row_key']))

    def import_sqlite(self,path:Path,*,source_id,account_key,binding,kind='native',cut=None,tables=None,row_windows=None,fault=None,cancelled=lambda:False,progress=None):
        if Path(path).resolve()==self.path:raise ValueError('cannot import the writable catalog into itself')
        if kind=='native':
            from .native_sqlite import decoded_connection
            source=decoded_connection(Path(path),cancelled=cancelled)
        else:
            source=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        source.row_factory=sqlite3.Row
        old=self.db.execute('SELECT * FROM source WHERE id=?',(source_id,)).fetchone()
        if old and (old['binding']!=binding or old['account_key']!=account_key or old['parser_version']!=VERSION or old['kind']!=kind):
            source.close();raise ValueError('source binding/parser changed; explicit new generation required')
        scanned=added=messages=changed_heads=schemas_added=0
        previous_cache=self.db.execute('PRAGMA cache_size').fetchone()[0]
        if not old:self.db.execute('PRAGMA cache_size=-262144')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR IGNORE INTO source VALUES(?,?,?,?,?,NULL)',(source_id,account_key,kind,binding,VERSION))
            cross_source=self.db.execute('SELECT 1 FROM source WHERE account_key=? AND id!=? LIMIT 1',(account_key,source_id)).fetchone() is not None
            initial_account=not old and not cross_source
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS next_dirty_fingerprints(digest BLOB PRIMARY KEY) WITHOUT ROWID')
            self.db.execute('DELETE FROM next_dirty_fingerprints')
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS next_dirty_published(id INTEGER PRIMARY KEY)')
            self.db.execute('DELETE FROM next_dirty_published')
            # The receipt is committed with all rows, never before them.
            capture=self.db.execute('INSERT INTO capture(source_id,cut_json,finished) VALUES(?,?,?)',
                                    (source_id,canonical(cut or {}),time.time())).lastrowid
            candidates=list(source.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"))
            virtuals=[r['name'] for r in candidates if 'CREATE VIRTUAL TABLE' in r['sql'].upper()]
            for candidate in candidates:
                table,ddl=candidate['name'],candidate['sql']
                if table.startswith('sqlite_') or any(table==v or table.startswith(v+'_') for v in virtuals):continue
                info=list(source.execute('PRAGMA table_info('+quote(table)+')'))
                columns=[r['name'] for r in info]
                self._column_cache[(source_id,table)]=columns
                pk=[r['name'] for r in sorted(info,key=lambda r:r['pk']) if r['pk']]
                schema=canonical({'ddl':ddl,'columns':columns,'primary_key':pk})
                previous=self.db.execute('SELECT schema_json FROM source_table WHERE source_id=? AND name=?',(source_id,table)).fetchone()
                if previous and previous[0]!=schema:raise ValueError('source table schema drift; explicit new baseline required')
                schemas_added+=self.db.execute('INSERT OR IGNORE INTO source_table VALUES(?,?,?)',(source_id,table,schema)).rowcount
                if tables is not None and table not in tables:continue
                if row_windows is not None and table not in row_windows:continue
                window=row_windows.get(table) if row_windows is not None else None
                rowid_alias=next((name for name in ('_rowid_','rowid','oid') if name not in columns),None)
                rowid_alias=rowid_alias if 'WITHOUT ROWID' not in ddl.upper() else None
                # SQLite permits NULL in some non-INTEGER rowid-table primary
                # keys. Those rows need their actual rowid as a disambiguator.
                needs_rowid=not pk or any(not item['notnull'] for item in info if item['pk'])
                if needs_rowid and rowid_alias is None:raise ValueError('source row identity is not unambiguous')
                selection=(quote(rowid_alias)+' AS "__qq_rowid__",*') if needs_rowid else '*'
                if window is not None and rowid_alias is not None and 'WITHOUT ROWID' not in ddl.upper():
                    def selected_rows():
                        for offset in range(0,len(window),500):
                            batch=window[offset:offset+500]
                            yield from source.execute('SELECT '+selection+' FROM '+quote(table)+' WHERE '+quote(rowid_alias)+' IN ('+','.join('?' for _ in batch)+')',batch)
                else:
                    def selected_rows():yield from source.execute('SELECT '+selection+' FROM '+quote(table))
                for row in selected_rows():
                    if cancelled():raise InterruptedError('import cancelled')
                    fields={name:row[name] for name in columns}
                    key_values=[fields[k] for k in pk] if pk else [row['__qq_rowid__']]
                    if pk and any(value is None for value in key_values):key_values=['qq-null-primary-key-rowid',row['__qq_rowid__'],*key_values]
                    row_key=hashlib.sha256(codec.encode(key_values).encode()).digest()
                    encoded=codec.encode(tuple(fields.values()))
                    row_hash=hashlib.sha256(encoded.encode()).digest()
                    head=self.db.execute('SELECT version_id,row_hash FROM row_head WHERE source_id=? AND source_table=? AND row_key=?',(source_id,table,row_key)).fetchone() if old else None
                    scanned+=1
                    if progress and scanned%5000==0:progress(scanned,added)
                    if head and head['row_hash']==row_hash:continue
                    changed_heads+=1
                    new_normalized=None
                    previous_version=self.db.execute('SELECT id FROM raw_version WHERE source_id=? AND source_table=? AND row_key=? AND row_hash=?',(source_id,table,row_key,row_hash)).fetchone() if old else None
                    if previous_version:
                        version=previous_version[0]
                    else:
                        payload=self.pool.intern_bytes(codec.encode_binary(tuple(fields.values())))
                        version=self.db.execute('INSERT INTO raw_version(source_id,source_table,row_key,row_hash,payload_id,capture_id) VALUES(?,?,?,?,?,?)',
                          (source_id,table,row_key,row_hash,payload,capture)).lastrowid
                        added+=1
                        normalized=normalize(table,fields)
                        if normalized:
                            message_kind,data=normalized
                            new_normalized=data
                            text=data.get('text') or ''
                            body=self._body(text)
                            normalized_id=self.pool.intern_json(canonical({'schema':'qq-normalization-replay/1',
                              'parser_version':VERSION,'sha256':hashlib.sha256(canonical(data).encode()).hexdigest()}))
                            chat='peer' if message_kind=='c2c' else 'group'
                            self.db.execute('INSERT INTO message_version VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                              (version,message_kind,normalized_id,body,data.get('timestamp'),str(data.get(chat+'_qq') or ''),
                               str(data.get(chat+'_uid',data.get('group_id')) or ''),str(data.get('sender_qq') or ''),
                               str(data.get('sender_uid') or ''),data.get('direction'),data.get('msg_type'),
                               data.get('historic_sender_label'),data.get('historic_sender_label_source'),str(data.get('msg_id',0))))
                            messages+=1
                    normalized_row=self.db.execute('SELECT kind,normalized_id FROM message_version WHERE version_id=?',(version,)).fetchone()
                    if normalized_row:
                        old_head=self.db.execute('SELECT fingerprint,published_id FROM message_head WHERE source_id=? AND source_table=? AND row_key=?',(source_id,table,row_key)).fetchone() if old else None
                        if old_head:self.db.execute('INSERT OR IGNORE INTO next_dirty_published VALUES(?)',(old_head['published_id'],))
                        if cross_source and old_head:self.db.execute('INSERT OR IGNORE INTO next_dirty_fingerprints VALUES(?)',(old_head[0],))
                        data=new_normalized if new_normalized is not None else self.normalized(version)
                        digest=fingerprint(normalized_row['kind'],data)
                        if cross_source:self.db.execute('INSERT OR IGNORE INTO next_dirty_fingerprints VALUES(?)',(digest,))
                        logical='obs_'+hashlib.sha256(source_id.encode()+table.encode()+row_key).hexdigest()
                        if not initial_account:self.db.execute('INSERT OR IGNORE INTO next_dirty_published VALUES(?)',(self._published_id(logical),))
                        self.db.execute('''INSERT INTO message_head VALUES(?,?,?,?,?,?,?,?,1)
                          ON CONFLICT(source_id,source_table,row_key) DO UPDATE SET version_id=excluded.version_id,fingerprint=excluded.fingerprint''',
                          (source_id,table,row_key,account_key,version,digest,logical,self._published_id(logical)))
                    self.db.execute('INSERT INTO row_head VALUES(?,?,?,?,?) ON CONFLICT(source_id,source_table,row_key) DO UPDATE SET version_id=excluded.version_id,row_hash=excluded.row_hash',
                                    (source_id,table,row_key,version,row_hash))
                    if fault:fault('after_row')
            if not changed_heads and not schemas_added and old:
                self.db.rollback()
                return {'rows_scanned':scanned,'versions_added':0,'message_versions_added':0,'no_op':True}
            if cross_source:self._regroup(account_key,(row[0] for row in self.db.execute('SELECT digest FROM next_dirty_fingerprints')),capture)
            self._sync_projection(account=account_key) if initial_account else self._sync_projection(dirty=True)
            self.db.execute('INSERT INTO capture_totals VALUES(?,?,?,?)',(capture,scanned,added,changed_heads))
            self.db.execute('UPDATE source SET latest_capture=? WHERE id=?',(capture,source_id))
            if fault:fault('before_commit')
            self.db.commit()
            return {'rows_scanned':scanned,'versions_added':added,'message_versions_added':messages,'no_op':False}
        except BaseException:
            self.db.rollback();raise
        finally:
            source.close()
            self.db.execute('PRAGMA cache_size='+str(previous_cache))
            if not old:self.db.execute('PRAGMA shrink_memory')

    def summary(self):
        return {'schema':SCHEMA,**{name:self.db.execute('SELECT count(*) FROM '+name).fetchone()[0]
          for name in ['source','raw_version','message_version','content_payload','capture']},
          'logical_messages':self.db.execute('SELECT count(*) FROM current_message').fetchone()[0]}

    def verify(self,*,cancelled=lambda:False,progress=None):
        if cancelled():raise InterruptedError('verification cancelled')
        self.db.set_progress_handler(lambda:int(cancelled()),10000)
        try:return self._verify(cancelled=cancelled,progress=progress)
        except sqlite3.OperationalError as exc:
            if cancelled() and getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT:raise InterruptedError('verification cancelled') from exc
            raise
        finally:self.db.set_progress_handler(None,0)

    def _verify(self,*,cancelled,progress):
        if progress:progress('sqlite_structure_and_references',0)
        if self.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('catalog integrity failure')
        if list(self.db.execute('PRAGMA foreign_key_check')):raise ValueError('catalog foreign key failure')
        mismatch=self.db.execute('''SELECT 1 FROM message_head h LEFT JOIN current_message c ON c.id=h.published_id
          WHERE h.representative=1 AND (c.id IS NULL OR c.version_id!=h.version_id) LIMIT 1''').fetchone()
        extra=self.db.execute('''SELECT 1 FROM current_message c WHERE NOT EXISTS
          (SELECT 1 FROM message_head h WHERE h.published_id=c.id AND h.representative=1 AND h.version_id=c.version_id) LIMIT 1''').fetchone()
        if mismatch or extra:raise ValueError('current message projection mismatch')
        summaries=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_summary'").fetchone()
        if summaries:
            expected='SELECT account_key,kind,chat_qq,chat_uid,count(*),min(timestamp),max(timestamp) FROM current_message GROUP BY account_key,kind,chat_qq,chat_uid'
            actual='SELECT account_key,kind,chat_qq,chat_uid,message_count,first_timestamp,last_timestamp FROM conversation_summary'
            if self.db.execute('SELECT 1 FROM ('+expected+' EXCEPT '+actual+') LIMIT 1').fetchone() or self.db.execute('SELECT 1 FROM ('+actual+' EXCEPT '+expected+') LIMIT 1').fetchone():
                raise ValueError('conversation summary mismatch')
        if progress:progress('payloads',0)
        pool=self.pool.verify()
        rows=0
        columns={(row['source_id'],row['name']):json.loads(row['schema_json'])['columns'] for row in self.db.execute('SELECT source_id,name,schema_json FROM source_table')}
        # Stream each raw row and its interpretation once. Re-querying/decompressing
        # raw payloads in a second pass makes multi-million-row restore needlessly slow.
        if progress:progress('raw_and_interpretations',0)
        query='''SELECT r.id,r.source_id,r.source_table,r.row_hash,r.payload_id,
          p.kind AS raw_kind,p.codec AS raw_codec,p.data AS raw_data,p.byte_length AS raw_size,
          m.normalized_id,m.body_id,n.kind AS norm_kind,n.codec AS norm_codec,n.data AS norm_data,n.byte_length AS norm_size,
          b.kind AS body_kind,b.codec AS body_codec,b.data AS body_data,b.byte_length AS body_size
          FROM raw_version r NOT INDEXED JOIN content_payload p ON p.id=r.payload_id
          LEFT JOIN message_version m ON m.version_id=r.id LEFT JOIN content_payload n ON n.id=m.normalized_id
          LEFT JOIN content_payload b ON b.id=m.body_id ORDER BY r.id'''
        for row in self.db.execute(query):
            if cancelled():raise InterruptedError('verification cancelled')
            payload=self.pool._read(row['payload_id'],row['raw_codec'],row['raw_data'],row['raw_size'])
            values=codec.decode_binary(payload) if row['raw_kind']=='bytes' else codec.decode(payload.decode('utf-8'))
            exact=codec.encode(values)
            if hashlib.sha256(exact.encode()).digest()!=row['row_hash']:raise ValueError('raw row hash mismatch')
            if row['normalized_id'] is not None:
                if row['norm_kind']!='text' or row['body_kind']!='text':raise ValueError('interpretation/body kind mismatch')
                document=json.loads(self.pool._read(row['normalized_id'],row['norm_codec'],row['norm_data'],row['norm_size']).decode('utf-8'))
                if document.get('schema')=='qq-normalization-replay/1':
                    if document.get('parser_version')!=VERSION:raise ValueError('normalization replay version mismatch')
                    result=normalize(row['source_table'],dict(zip(columns[(row['source_id'],row['source_table'])],values)))
                    if result is None:raise ValueError('normalization replay is unavailable')
                    normalized=result[1]
                    if hashlib.sha256(canonical(normalized).encode()).hexdigest()!=document['sha256']:raise ValueError('normalization replay differs from observed interpretation')
                else:normalized=document
                body=self.pool._read(row['body_id'],row['body_codec'],row['body_data'],row['body_size']).decode('utf-8')
                if (normalized.get('text') or '')!=body:raise ValueError('search body mismatch')
            rows+=1
            if progress and rows%10000==0:progress('raw_and_interpretations',rows)
        if not self.readonly:
            self.db.execute('SAVEPOINT fts_verify')
            try:self.db.execute("INSERT INTO body_fts(body_fts,rank) VALUES('integrity-check',1)")
            finally:
                self.db.execute('ROLLBACK TO fts_verify');self.db.execute('RELEASE fts_verify')
        return {**pool,'raw_versions_verified':rows,'fts_integrity_checked':not self.readonly,**self.summary()}

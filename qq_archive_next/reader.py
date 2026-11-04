"""Expose the new archive through the existing read-only viewer API."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from qq_nt_archive_viewer.archive_store import ArchiveStore, ArchiveError, Conversation
from .catalog import Catalog
from . import codec
from .content_pool import sql_text_expression


@lru_cache(maxsize=8)
def _historic_media_summary(path,signature):
    """Frozen sidecars share this small summary across background read jobs."""
    db=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)
    try:
        columns={r[1] for r in db.execute('PRAGMA table_info(assets)')}
        objects=frozenset(r[0] for r in db.execute('SELECT sha256 FROM assets')) if 'sha256' in columns else frozenset(Path(r[0]).name for r in db.execute('SELECT archive_relative FROM assets'))
        return objects,db.execute('SELECT count(*) FROM message_media').fetchone()[0]
    finally:db.close()


class NextArchiveStore(ArchiveStore):
    def __init__(self,manifest_path,account_label=None,*,deep_validation=False,cache_dir=None,cancelled=None):
        self.manifest_path=Path(manifest_path).resolve(strict=True)
        self.manifest=json.loads(self.manifest_path.read_text(encoding='utf-8'))
        if self.manifest.get('schema') not in {'qq-next-viewer/1','qq-next-viewer/2'}:raise ArchiveError('unsupported live archive')
        if (self.manifest_path.parent/'BUILDING.json').exists() or (self.manifest_path.parent/'RESTORING.json').exists():
            raise ArchiveError('归档整合或恢复尚未完成')
        relative=self.manifest.get('catalog')
        if not isinstance(relative,str) or Path(relative).is_absolute() or '\\' in relative or ':' in relative or '..' in Path(relative).parts:
            raise ArchiveError('unsafe live catalog path')
        path=(self.manifest_path.parent/relative).resolve(strict=True)
        if not path.is_relative_to(self.manifest_path.parent):raise ArchiveError('live catalog escapes state')
        self.catalog=Catalog(path,readonly=True)
        self.message_connection=self.catalog.db
        self.map_connection=self.message_connection
        self.identity_connection=None
        self.identity_schema=self.identity_coverage=self.identity_snapshot_coverage=self.identity_snapshot_id=None
        self._snapshot_contact_cache={};self._snapshot_group_title_cache={};self._snapshot_member_cache={}
        self.account_id=str(account_label or self.manifest.get('account_id') or self.manifest['account_key'])
        self.storage_account_id=self.account_id
        self.catalog_account_key=str(self.manifest['account_key'])
        self.archive_schema=self.manifest['schema']
        self.cache_dir=None
        self._conversation_cache=self._conversation_index=None
        self.message_connection.execute('PRAGMA query_only=OFF')
        account="'"+self.catalog_account_key.replace("'","''")+"'"
        norm='(SELECT '+sql_text_expression('n')+' FROM content_payload n WHERE n.id=v.normalized_id)'
        body='(SELECT '+sql_text_expression('b')+' FROM content_payload b WHERE b.id=v.body_id)'
        for table,kind,chat in [('c2c_messages','c2c','peer'),('group_messages','group','group')]:
            uid='peer_uid' if kind=='c2c' else 'group_id'
            self.message_connection.execute(f'''CREATE TEMP VIEW {table} AS
              SELECT v.id AS msg_id,v.timestamp,v.direction,v.sender_uid,
                v.sender_qq,v.msg_type,v.chat_uid AS {uid},v.chat_qq AS {chat}_qq,
                {body} AS text,NULL AS content,NULL AS content_type,NULL AS proto_ver,
                NULL AS inner_ts,NULL AS subtype,'replay_on_demand' AS parse_status
              FROM current_message v NOT INDEXED WHERE v.account_key={account} AND v.kind='{kind}' ''')
        self.message_connection.execute('PRAGMA query_only=ON')
        if cancelled:self.message_connection.set_progress_handler(lambda:int(cancelled()),10000)
        self._data_version=self.message_connection.execute('PRAGMA data_version').fetchone()[0]
        self.cancelled=cancelled or (lambda:False)
        self._identity_owner=None
        self._legacy={}
        self.identity_dependency_error=None
        if self.archive_schema=='qq-next-viewer/2':
            from .config import inside
            try:
                for source in self.manifest.get('legacy_sources',[]):
                    if source['account_key']!=self.catalog_account_key or source['id'] in self._legacy:raise ArchiveError('历史来源绑定无效')
                    path=inside(self.manifest_path.parent,source['manifest'])
                    self._legacy[source['id']]=ArchiveStore(path,account_label=self.account_id,cancelled=self.cancelled)
            except BaseException:
                self.close();raise
        self._optional_historic_snapshot()
        if deep_validation:self.verify_integrity()

    def _optional_historic_snapshot(self):
        """Exact old UID/QQ lookups retain their original snapshot labels/date."""
        root=self.manifest_path.parent
        try:
            labels=root/'private/account_labels.private.json'
            if self.account_id==self.catalog_account_key and labels.is_file():
                document=json.loads(labels.read_text(encoding='utf-8'))
                if document.get('schema')=='qq-account-labels-private/1':
                    matching=[item['account_label'] for item in document.get('accounts',[]) if item.get('account_key')==self.catalog_account_key]
                    if len(matching)==1:self.account_id=self.storage_account_id=str(matching[0])
            if self._legacy:
                previous=next(iter(self._legacy.values()))
                self.identity_connection=previous.identity_connection
                self.identity_schema=previous.identity_schema
                self.identity_coverage=previous.identity_coverage
                self.identity_snapshot_id=previous.identity_snapshot_id
                self.identity_snapshot_coverage=previous.identity_snapshot_coverage
                return
            if not (root/'config.private.json').is_file() or not (root/'OWNER.json').is_file():return
            config=json.loads((root/'config.private.json').read_text(encoding='utf-8'))
            owner=json.loads((root/'OWNER.json').read_text(encoding='utf-8'))
            if config.get('state_id')!=owner.get('id'):return
            matches=[item for item in config.get('sources',[]) if item.get('account_key')==self.catalog_account_key and item.get('legacy_archive')]
            if not matches:return
            path=Path(matches[0]['legacy_archive'])
            if not path.is_absolute() or (os.name=='nt' and (len(path.drive)!=2 or not path.drive.endswith(':'))):return
            if not path.is_file():self.identity_dependency_error='历史资料快照不可用';return
            previous=ArchiveStore(path,account_label=self.account_id,deep_validation=False,cancelled=self.cancelled)
            if previous.identity_schema!='qq-identity-index/2' or not previous.identity_snapshot_id:
                previous.close();return
            self._identity_owner=previous
            self.identity_connection=previous.identity_connection
            self.identity_schema=previous.identity_schema
            self.identity_snapshot_id=previous.identity_snapshot_id
            self.identity_snapshot_coverage=previous.identity_snapshot_coverage
        except (OSError,ValueError,ArchiveError,sqlite3.Error) as exc:
            self.identity_dependency_error=str(exc)

    @contextmanager
    def _snapshot(self):
        nested=self.message_connection.in_transaction
        if not nested:self.message_connection.execute('BEGIN')
        try:yield
        finally:
            if not nested:self.message_connection.rollback()

    def close(self):
        self.catalog.close()
        if self._identity_owner:self._identity_owner.close()
        for previous in self._legacy.values():previous.close()

    def _aliases(self,ids,source_table=None):
        if not ids:return []
        condition=' AND h.source_table IN (?,?)' if source_table else ''
        raw_table={'c2c_messages':'c2c_msg_table','group_messages':'group_msg_table'}.get(source_table,source_table)
        # Page IDs must drive this lookup; account estimates otherwise scan
        # millions of unrelated heads for a small IN list on a fresh catalog.
        return list(self.message_connection.execute('''SELECT h.published_id,h.source_id,h.source_table,h.version_id,v.source_msg_id,s.kind
          FROM message_head h INDEXED BY heads_published JOIN message_version v ON v.version_id=h.version_id JOIN source s ON s.id=h.source_id
          WHERE h.account_key=? AND h.published_id IN ('''+','.join('?' for _ in ids)+')'+condition,
          (self.catalog_account_key,*ids,*([source_table,raw_table] if source_table else []))))

    def verify_integrity(self):
        with self._snapshot():return self.catalog.verify(cancelled=self.cancelled)

    def overview(self):
        with self._snapshot():
            has_summary=self.message_connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_summary'").fetchone()
            sql='SELECT kind,sum(message_count) FROM conversation_summary WHERE account_key=? GROUP BY kind' if has_summary else 'SELECT kind,count(*) FROM current_message WHERE account_key=? GROUP BY kind'
            counts={row[0]:row[1] for row in self.message_connection.execute(sql,(self.catalog_account_key,))}
            objects=set();media_links=0
            if (self.manifest_path.parent/'assets.sqlite').is_file():
                from .media import connect
                assets=connect(self.manifest_path.parent,readonly=True)
                try:objects.update(r[0] for r in assets.execute('SELECT DISTINCT sha256 FROM observation WHERE account_key=?',(self.catalog_account_key,)))
                finally:assets.close()
            for previous in self._legacy.values():
                path=Path(previous.map_connection.execute('PRAGMA database_list').fetchone()[2]);stat=path.stat()
                historic,count=_historic_media_summary(str(path),(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
                objects.update(historic);media_links+=count
            return {'account_id':self.account_id,'c2c_messages':counts.get('c2c',0),'group_messages':counts.get('group',0),
                    'media_links':media_links,'media_objects':len(objects),'identity_labels':(self.identity_coverage or {}).get('labelled_messages',0),'identity_coverage':self.identity_coverage,'identity_snapshot_coverage':self.identity_snapshot_coverage,
                    'identity_snapshot_id':self.identity_snapshot_id,'optional_identity_error':self.identity_dependency_error,'manifest':str(self.manifest_path)}

    def revision(self):
        return self.message_connection.execute('SELECT coalesce(max(id),0) FROM capture').fetchone()[0]

    def summaries(self):
        with self._snapshot():return self.revision(),self.overview(),self.conversations()

    def _identity_labels_for_messages(self,source_table,message_ids):
        ids=list(message_ids)
        if not ids or source_table!='group_messages':return {}
        rows=self.message_connection.execute('''SELECT id,historic_label,historic_label_source FROM current_message NOT INDEXED
          WHERE account_key=? AND id IN ('''+','.join('?' for _ in ids)+')',(self.catalog_account_key,*ids))
        labels={r[0]:(r[1],r[2]) for r in rows if r[1]}
        aliases=self._aliases(ids,source_table)
        for identity,previous in self._legacy.items():
            mapped={int(r['source_msg_id']):r['published_id'] for r in aliases if r['source_id']==identity}
            for old,value in previous._identity_labels_for_messages(source_table,mapped).items():labels.setdefault(mapped[old],value)
        return labels

    def _rows_to_messages(self,source_table,rows):
        rows=list(rows)
        messages=super()._rows_to_messages(source_table,rows)
        if not messages:return []
        ids=[message.msg_id for message in messages]
        versions={r[0]:r[1] for r in self.message_connection.execute('SELECT id,version_id FROM current_message NOT INDEXED WHERE id IN ('+','.join('?' for _ in ids)+')',ids)}
        return [replace(message,content=self.catalog.normalized(versions[message.msg_id]).get('content') or '') for message in messages]

    def media_for_messages(self,source_table,messages):
        from .media import links
        with self._snapshot():
            messages=list(messages)
            ids=[message.msg_id for message in messages]
            if not ids:return {}
            aliases=self._aliases(ids,source_table)
            result=links(self.manifest_path.parent,self.catalog_account_key,
              [(r['published_id'],source_table,self.catalog.native_fields(r['version_id'])) for r in aliases if r['kind']=='native'])
            messages_by_id={m.msg_id:m for m in messages}
            for identity,previous in self._legacy.items():
                mapped={int(r['source_msg_id']):r['published_id'] for r in aliases if r['source_id']==identity}
                old_messages=[replace(messages_by_id[new],msg_id=old) for old,new in mapped.items()]
                for old,items in previous.media_for_messages(source_table,old_messages).items():
                    for item in items:
                        if item.media_type=='voice' and not item.playable and item.verification in {'verified','verified_exact','verified_hash','verified_content'} and item.primary:
                            item=replace(item,playable=item.primary)
                        result.setdefault(mapped[old],[]).append(replace(item,msg_id=mapped[old],note=(item.note+' · 历史来源 '+identity).strip(' ·')))
            for msg_id,items in result.items():
                unique={}
                for item in items:
                    assets=tuple(asset.path.name if asset and asset.path else (asset.archive_relative if asset else None) for asset in (item.primary,item.preview,item.playable,item.candidate))
                    key=(item.segment_index,item.media_type,item.role,item.verification,assets)
                    unique.setdefault(key,item)
                result[msg_id]=list(unique.values())
            return result

    def conversations(self,*args,**kwargs):
        with self._snapshot():
            version=self.revision()
            if version!=self._data_version:
                self._conversation_cache=self._conversation_index=None;self._data_version=version
            return super().conversations(*args,**kwargs)

    def _load_conversations(self):
        if self._conversation_cache is not None:return self._conversation_cache
        exists=self.message_connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_summary'").fetchone()
        if not exists:return super()._load_conversations()
        rows=list(self.message_connection.execute('SELECT * FROM conversation_summary WHERE account_key=?',(self.catalog_account_key,)))
        mappings={}
        for row in rows:
            if row['chat_qq'] and row['chat_uid']:
                mappings.setdefault((row['kind'],row['chat_uid']),set()).add(row['chat_qq'])
        buckets={}
        for row in rows:
            qq=row['chat_qq'];uid=row['chat_uid'];kind=row['kind']
            if not qq and uid and len(mappings.get((kind,uid),()))==1:qq=next(iter(mappings[(kind,uid)]))
            if not qq and not uid:continue
            key=(kind,'qq' if qq else 'uid',str(qq) if qq else uid)
            bucket=buckets.setdefault(key,{'count':0,'first':[],'last':[],'uids':set()})
            bucket['count']+=row['message_count']
            if row['first_timestamp'] is not None:bucket['first'].append(row['first_timestamp'])
            if row['last_timestamp'] is not None:bucket['last'].append(row['last_timestamp'])
            if uid:bucket['uids'].add(uid)
        values=[]
        for (kind,id_kind,identity),bucket in buckets.items():
            table='c2c_messages' if kind=='c2c' else 'group_messages'
            label_kind='私聊' if kind=='c2c' else '群聊'
            qq_column='peer_qq' if kind=='c2c' else 'group_qq';uid_column='peer_uid' if kind=='c2c' else 'group_id'
            qq=int(identity) if id_kind=='qq' else None;uids=tuple(sorted(bucket['uids']))
            values.append(Conversation(table,label_kind,qq_column if qq else uid_column,identity,
              self._conversation_label(label_kind,qq,uids),bucket['count'],min(bucket['first']) if bucket['first'] else None,
              max(bucket['last']) if bucket['last'] else None,qq_column,uid_column,qq,uids))
        values.sort(key=lambda item:(item.last_timestamp is not None,item.last_timestamp or 0,item.label),reverse=True)
        self._set_conversations(values)
        return values

    def latest_messages(self,*args,**kwargs):
        with self._snapshot():return super().latest_messages(*args,**kwargs)
    def older_messages(self,conversation,first_message):
        with self._snapshot():return self._message_query(conversation,before=(first_message.timestamp,first_message.msg_id))
    def newer_messages(self,conversation,last_message):
        with self._snapshot():return self._message_query(conversation,after=(last_message.timestamp,last_message.msg_id))
    def _message_query(self,conversation,*,before=None,after=None,limit=200):
        if before and after:raise ArchiveError('conflicting page cursors')
        kind='c2c' if conversation.source_table=='c2c_messages' else 'group'
        clause,parameters=self._conversation_clause(conversation)
        for original,replacement in [('peer_qq','chat_qq'),('group_qq','chat_qq'),('peer_uid','chat_uid'),('group_id','chat_uid')]:
            clause=clause.replace(original,replacement)
        parameters=[self.catalog_account_key,kind,*parameters]
        condition='account_key=? AND kind=? AND ('+clause+')'
        if before:
            if before[0] is None:condition+=' AND timestamp IS NULL AND id<?';parameters.append(before[1])
            else:condition+=' AND (timestamp<? OR (timestamp=? AND id<?) OR timestamp IS NULL)';parameters.extend((before[0],before[0],before[1]))
        if after:
            if after[0] is None:condition+=' AND (timestamp IS NOT NULL OR (timestamp IS NULL AND id>?))';parameters.append(after[1])
            else:condition+=' AND (timestamp>? OR (timestamp=? AND id>?))';parameters.extend((after[0],after[0],after[1]))
        order='ASC' if after else 'DESC'
        ids=[r[0] for r in self.message_connection.execute(f'SELECT id FROM current_message WHERE {condition} ORDER BY timestamp {order},id {order} LIMIT ?',(*parameters,limit))]
        if not ids:return []
        columns=self._message_columns(conversation.source_table)
        rows=list(self.message_connection.execute(f'''SELECT {columns} FROM {conversation.source_table}
          WHERE msg_id IN ('''+','.join('?' for _ in ids)+') ORDER BY timestamp,msg_id',ids))
        return self._rows_to_messages(conversation.source_table,rows)

    def messages_around(self,conversation,msg_id,limit=200):
        with self._snapshot():
            target=self.message_connection.execute('SELECT timestamp FROM current_message WHERE id=? AND account_key=?',(msg_id,self.catalog_account_key)).fetchone()
            if target is None:return []
            timestamp=target[0]
            earlier=self._message_query(conversation,before=(timestamp,msg_id),limit=limit//2)
            later=self._message_query(conversation,after=(timestamp,msg_id-1),limit=limit-len(earlier))
            return earlier+later
    def search(self,*args,**kwargs):
        with self._snapshot():return super().search(*args,**kwargs)

    def _search_rows(self,source_table,qq_column,uid_column,phrase,row_limit):
        parameters=[]
        expression=sql_text_expression('p')
        if len(phrase)>=3 and '\0' not in phrase:
            grams=list(dict.fromkeys(phrase[i:i+3] for i in range(min(len(phrase)-2,32))))
            match=' AND '.join('"'+gram.replace('"','""')+'"' for gram in grams)
            candidates=f'''SELECT f.rowid AS id,{expression} AS text FROM
              (SELECT rowid FROM body_fts WHERE body_fts MATCH ?) f
              CROSS JOIN content_payload p ON p.id=f.rowid'''
            parameters.append(match)
        else:
            # Restrict a two-character scan to this account's distinct bodies.
            candidates=f'''SELECT b.body_id AS id,{expression} AS text FROM
              (SELECT DISTINCT body_id FROM current_message INDEXED BY current_body WHERE account_key=?) b
              CROSS JOIN content_payload p ON p.id=b.body_id'''
            parameters.append(self.catalog_account_key)
        parameters.extend((phrase,self.catalog_account_key,'c2c' if source_table=='c2c_messages' else 'group',row_limit))
        # Materialize the small, already-matched body set before joining messages.
        # Otherwise SQLite can decode all message bodies before applying ID IN.
        return list(self.message_connection.execute(f'''WITH candidates AS MATERIALIZED ({candidates}),
          matched AS MATERIALIZED (SELECT id,text FROM candidates WHERE instr(lower(text),lower(?))>0)
          SELECT c.id AS msg_id,c.timestamp,c.direction,c.sender_qq,c.sender_uid,b.text,
            c.chat_qq,c.chat_uid FROM matched b CROSS JOIN current_message c INDEXED BY current_body
            ON c.account_key=? AND c.body_id=b.id WHERE c.kind=?
          ORDER BY c.timestamp DESC,c.id DESC LIMIT ?''',parameters))

    def observations(self,published_id):
        with self._snapshot():
            rows=self._aliases([published_id])
            result=[]
            for row in rows:
                for version in self.message_connection.execute('''SELECT id,row_hash,payload_id,capture_id FROM raw_version
                  WHERE source_id=? AND source_table=? AND row_key=(SELECT row_key FROM raw_version WHERE id=?) ORDER BY id''',
                  (row['source_id'],row['source_table'],row['version_id'])):
                    observation={'source_id':row['source_id'],'table':row['source_table'],'source_msg_id':row['source_msg_id'],
                      'version_id':version['id'],'capture_id':version['capture_id'],'row_hash':bytes(version['row_hash']).hex(),
                      'typed_native_row':[codec.tagged(v) for v in self.catalog.raw_values(version['payload_id'])]}
                    capture=self.message_connection.execute('SELECT cut_json,finished FROM capture WHERE id=?',(version['capture_id'],)).fetchone()
                    observation['capture']={'cut':json.loads(capture[0]),'finished':capture[1]}
                    previous=self._legacy.get(row['source_id'])
                    if previous:
                        observation['historic_provenance']=[{key:(bytes(value).hex() if isinstance(value,bytes) else value) for key,value in dict(p).items()}
                          for p in previous.message_connection.execute('''SELECT p.*,s.source_kind,s.package_id,s.structured_relative,s.structured_sha256
                            FROM message_provenance p JOIN source_runs s USING(account_run_id) WHERE p.published_msg_id=?''',(row['source_msg_id'],))]
                        observation['historic_manifest']=str(previous.manifest_path.relative_to(self.manifest_path.parent))
                        observation['historic_media_links']=[dict(p) for p in previous.map_connection.execute('SELECT * FROM message_media WHERE source_table=? AND msg_id=?',
                          ('c2c_msg_table' if row['source_table']=='c2c_messages' else 'group_msg_table',row['source_msg_id']))]
                        if previous.map_connection.execute("SELECT 1 FROM sqlite_master WHERE name='media_provenance'").fetchone():
                            observation['historic_media_provenance']=[dict(p) for p in previous.map_connection.execute('SELECT * FROM media_provenance WHERE published_msg_id=?',(row['source_msg_id'],))]
                        if previous.identity_connection and row['source_table']=='group_messages':
                            observation['historic_sender_label']=previous._identity_labels_for_messages('group_messages',[int(row['source_msg_id'])]).get(int(row['source_msg_id']))
                            if previous.identity_connection.execute("SELECT 1 FROM sqlite_master WHERE name='identity_provenance'").fetchone():
                                observation['historic_identity_provenance']=[dict(p) for p in previous.identity_connection.execute('''SELECT p.*,v.display_name,v.label_source
                                  FROM identity_provenance p JOIN label_values v USING(label_id) WHERE published_msg_id=?''',(row['source_msg_id'],))]
                    result.append(observation)
            return result

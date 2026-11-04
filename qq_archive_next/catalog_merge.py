"""Lossless merge of a frozen next catalog, including revisions and receipts.

Watermarks here apply only to our append-only evidence IDs, never QQ IDs/time.
Current heads are always reconciled, including a return to an earlier version.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .catalog import Catalog,SCHEMA,canonical


def _regroup_bulk(catalog,account,capture):
    """The catalog's exact folding rule, evaluated in batches for millions of heads."""
    db=catalog.db
    db.create_function('unified_shared',2,lambda a,d:'strict_'+hashlib.sha256(a.encode()+d).hexdigest(),deterministic=True)
    db.create_function('unified_observation',3,lambda s,t,k:'obs_'+hashlib.sha256(s.encode()+t.encode()+k).hexdigest(),deterministic=True)
    db.create_function('unified_published',1,catalog._published_id,deterministic=True)
    db.execute('''CREATE TEMP TABLE merge_groups AS SELECT h.fingerprint,
      count(*)>1 AND count(*)=count(DISTINCT h.source_id) AS fold
      FROM next_dirty_fingerprints d CROSS JOIN message_head h INDEXED BY heads_fingerprint
      ON h.account_key=? AND h.fingerprint=d.digest GROUP BY h.fingerprint''',(account,))
    db.execute('CREATE UNIQUE INDEX temp.merge_groups_key ON merge_groups(fingerprint)')
    db.execute('''CREATE TEMP TABLE merge_relink AS WITH candidates AS (
      SELECT h.*,CASE WHEN g.fold THEN unified_shared(h.account_key,h.fingerprint)
        ELSE unified_observation(h.source_id,h.source_table,h.row_key) END AS new_logical,
        CASE WHEN g.fold THEN row_number() OVER(PARTITION BY h.fingerprint ORDER BY s.kind,h.source_id,h.source_table,h.row_key)=1 ELSE 1 END AS new_representative
      FROM merge_groups g CROSS JOIN message_head h INDEXED BY heads_fingerprint ON h.account_key=? AND h.fingerprint=g.fingerprint
      JOIN source s ON s.id=h.source_id)
      SELECT *,unified_published(new_logical) AS new_published FROM candidates''',(account,))
    collision=db.execute('''SELECT 1 FROM merge_relink n JOIN message_head h INDEXED BY heads_published
      ON h.published_id=n.new_published WHERE h.logical_id!=n.new_logical LIMIT 1''').fetchone()
    if collision or db.execute('SELECT 1 FROM merge_relink GROUP BY new_published HAVING count(DISTINCT new_logical)>1 LIMIT 1').fetchone():raise ValueError('published message ID collision')
    db.execute('''INSERT INTO fold_history SELECT ?,version_id,logical_id,new_logical,
      CASE WHEN new_logical LIKE 'strict_%' THEN 'strict_one_to_one_cross_source' ELSE 'separate_observation' END
      FROM merge_relink WHERE logical_id!=new_logical''',(capture,))
    db.execute('INSERT OR IGNORE INTO next_dirty_published SELECT published_id FROM merge_relink')
    db.execute('INSERT OR IGNORE INTO next_dirty_published SELECT new_published FROM merge_relink')
    db.execute('''INSERT INTO message_head SELECT source_id,source_table,row_key,account_key,version_id,fingerprint,
      new_logical,new_published,new_representative FROM merge_relink WHERE 1
      ON CONFLICT(source_id,source_table,row_key) DO UPDATE SET logical_id=excluded.logical_id,
      published_id=excluded.published_id,representative=excluded.representative
      WHERE message_head.logical_id!=excluded.logical_id OR message_head.representative!=excluded.representative''')
    db.execute('DROP TABLE merge_relink');db.execute('DROP TABLE merge_groups')


def merge(catalog:Catalog,snapshot,*,origin_state_id,snapshot_sha256,cancelled=lambda:False,progress=None):
    db=catalog.db;snapshot=Path(snapshot).resolve(strict=True)
    if catalog.readonly or snapshot==catalog.path:raise ValueError('merge requires a distinct writable destination')
    if cancelled():raise InterruptedError('catalog merge cancelled')
    db.execute('ATTACH DATABASE ? AS incoming',(snapshot.as_uri()+'?mode=ro&immutable=1',))
    previous_cache=db.execute('PRAGMA cache_size').fetchone()[0]
    started=time.monotonic();phase='validate';last=[0.0]
    def report(name=None):
        nonlocal phase
        if cancelled():raise InterruptedError('catalog merge cancelled')
        if name:phase=name
        now=time.monotonic()
        if progress and (name or now-last[0]>2):progress({'phase':phase,'seconds':round(now-started,3)});last[0]=now
    def check():
        if cancelled():return 1
        report();return 0
    db.set_progress_handler(check,100000)
    try:
        if db.execute("SELECT value FROM incoming.metadata WHERE key='schema'").fetchone()[0]!=SCHEMA:raise ValueError('unsupported incoming catalog')
        db.executescript('''CREATE TABLE IF NOT EXISTS unified_origin(
          id INTEGER PRIMARY KEY,state_id TEXT NOT NULL UNIQUE,pool_cursor INTEGER NOT NULL DEFAULT 0,
          raw_cursor INTEGER NOT NULL DEFAULT 0,capture_cursor INTEGER NOT NULL DEFAULT 0,fold_cursor INTEGER NOT NULL DEFAULT 0,
          closed INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS unified_payload(origin INTEGER NOT NULL REFERENCES unified_origin(id),
            old_id INTEGER NOT NULL,new_id INTEGER NOT NULL REFERENCES content_payload(id),PRIMARY KEY(origin,old_id)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS unified_version(origin INTEGER NOT NULL REFERENCES unified_origin(id),
            old_id INTEGER NOT NULL,new_id INTEGER NOT NULL REFERENCES raw_version(id),PRIMARY KEY(origin,old_id)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS unified_capture(origin INTEGER NOT NULL REFERENCES unified_origin(id),
            old_id INTEGER NOT NULL,new_id INTEGER NOT NULL REFERENCES capture(id),PRIMARY KEY(origin,old_id)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS unified_feed(id INTEGER PRIMARY KEY,origin INTEGER NOT NULL REFERENCES unified_origin(id),
            snapshot_sha256 TEXT NOT NULL,pool_max INTEGER NOT NULL,raw_max INTEGER NOT NULL,capture_max INTEGER NOT NULL,
            heads_changed INTEGER NOT NULL,finished REAL NOT NULL);''')
        db.execute('PRAGMA cache_size=-1048576')
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO unified_origin(state_id) VALUES(?)',(origin_state_id,))
        origin=db.execute('SELECT * FROM unified_origin WHERE state_id=?',(origin_state_id,)).fetchone();oid=origin['id']
        if origin['closed']:raise ValueError('this migration feed is closed; cannot overwrite live unified heads')
        maxima={name:db.execute('SELECT coalesce(max(id),0) FROM incoming.'+table).fetchone()[0] for name,table in [('pool','content_payload'),('raw','raw_version'),('capture','capture')]}
        if any(maxima[k]<origin[k+'_cursor'] for k in maxima):raise ValueError('incoming append-only evidence moved backwards')
        previous=db.execute('SELECT snapshot_sha256 FROM unified_feed WHERE origin=? ORDER BY id DESC LIMIT 1',(oid,)).fetchone()
        if previous and previous[0]==snapshot_sha256 and all(maxima[k]==origin[k+'_cursor'] for k in maxima):
            db.rollback();return {'origin':origin_state_id,'heads_changed':0,'no_op':True,'seconds':round(time.monotonic()-started,3)}
        sources=list(db.execute('SELECT * FROM incoming.source'))
        for row in sources:
            known=db.execute('SELECT * FROM source WHERE id=?',(row['id'],)).fetchone()
            if known and any(known[k]!=row[k] for k in ('account_key','kind','binding','parser_version')):raise ValueError('incoming source generation differs')
            db.execute('INSERT OR IGNORE INTO source VALUES(?,?,?,?,?,NULL)',tuple(row[k] for k in ('id','account_key','kind','binding','parser_version')))
        mismatch=db.execute('''SELECT 1 FROM incoming.source_table s JOIN source_table t ON t.source_id=s.source_id AND t.name=s.name
          WHERE t.schema_json!=s.schema_json LIMIT 1''').fetchone()
        if mismatch:raise ValueError('incoming source schema differs')
        db.execute('INSERT OR IGNORE INTO source_table SELECT * FROM incoming.source_table')

        report('copy_shared_content')
        db.execute('CREATE TEMP TABLE merge_new_payload(old_id INTEGER PRIMARY KEY)')
        db.execute('''INSERT INTO merge_new_payload SELECT p.id FROM incoming.content_payload p
          LEFT JOIN content_payload t ON t.kind=p.kind AND t.sha256=p.sha256 WHERE p.id>? AND t.id IS NULL''',(origin['pool_cursor'],))
        mismatch=db.execute('''SELECT 1 FROM incoming.content_payload p JOIN content_payload t ON t.kind=p.kind AND t.sha256=p.sha256
          WHERE p.id>? AND p.byte_length!=t.byte_length LIMIT 1''',(origin['pool_cursor'],)).fetchone()
        if mismatch:raise ValueError('shared content digest has different length')
        db.execute('''INSERT INTO content_payload(kind,sha256,byte_length,codec,data)
          SELECT p.kind,p.sha256,p.byte_length,p.codec,p.data FROM incoming.content_payload p JOIN merge_new_payload n ON n.old_id=p.id ORDER BY p.id''')
        db.execute('''INSERT INTO unified_payload SELECT ?,p.id,t.id FROM incoming.content_payload p
          JOIN content_payload t ON t.kind=p.kind AND t.sha256=p.sha256 WHERE p.id>?''',(oid,origin['pool_cursor']))
        # Recipes only follow the copied representation; an already interned
        # value may have an equally exact but differently framed recipe.
        db.execute('''INSERT INTO content_payload_reference SELECT p.new_id,r.slot,a.new_id
          FROM incoming.content_payload_reference r JOIN merge_new_payload n ON n.old_id=r.payload_id
          JOIN unified_payload p ON p.origin=? AND p.old_id=r.payload_id
          JOIN unified_payload a ON a.origin=? AND a.old_id=r.atom_id''',(oid,oid))
        db.execute('DROP TABLE merge_new_payload')

        report('copy_capture_receipts')
        for row in db.execute('SELECT * FROM incoming.capture WHERE id>? ORDER BY id',(origin['capture_cursor'],)):
            new=db.execute('INSERT INTO capture(source_id,cut_json,finished) VALUES(?,?,?)',(row['source_id'],row['cut_json'],row['finished'])).lastrowid
            db.execute('INSERT INTO unified_capture VALUES(?,?,?)',(oid,row['id'],new))
        db.execute('''INSERT INTO capture_totals SELECT m.new_id,t.rows_scanned,t.versions_added,t.heads_changed
          FROM incoming.capture_totals t JOIN unified_capture m ON m.origin=? AND m.old_id=t.capture_id WHERE t.capture_id>?''',(oid,origin['capture_cursor']))

        report('copy_raw_revisions')
        db.execute('''INSERT INTO raw_version(source_id,source_table,row_key,row_hash,payload_id,capture_id)
          SELECT r.source_id,r.source_table,r.row_key,r.row_hash,p.new_id,c.new_id FROM incoming.raw_version r
          JOIN unified_payload p ON p.origin=? AND p.old_id=r.payload_id JOIN unified_capture c ON c.origin=? AND c.old_id=r.capture_id
          WHERE r.id>? ORDER BY r.id''',(oid,oid,origin['raw_cursor']))
        db.execute('''INSERT INTO unified_version SELECT ?,r.id,v.id FROM incoming.raw_version r
          JOIN raw_version v ON v.source_id=r.source_id AND v.source_table=r.source_table AND v.row_key=r.row_key AND v.row_hash=r.row_hash
          WHERE r.id>?''',(oid,origin['raw_cursor']))
        columns='kind,timestamp,chat_qq,chat_uid,sender_qq,sender_uid,direction,msg_type,historic_label,historic_label_source,source_msg_id'
        db.execute('''INSERT INTO message_version(version_id,normalized_id,body_id,'''+columns+''')
          SELECT v.new_id,n.new_id,b.new_id,'''+','.join('m.'+k for k in columns.split(','))+''' FROM incoming.message_version m
          JOIN unified_version v ON v.origin=? AND v.old_id=m.version_id
          JOIN unified_payload n ON n.origin=? AND n.old_id=m.normalized_id
          JOIN unified_payload b ON b.origin=? AND b.old_id=m.body_id WHERE m.version_id>?''',(oid,oid,oid,origin['raw_cursor']))
        db.execute('''INSERT INTO fold_history SELECT c.new_id,v.new_id,h.previous_logical_id,h.logical_id,h.rule
          FROM incoming.fold_history h JOIN unified_capture c ON c.origin=? AND c.old_id=h.capture_id
          JOIN unified_version v ON v.origin=? AND v.old_id=h.version_id WHERE h.rowid>?''',(oid,oid,origin['fold_cursor']))
        fold_max=db.execute('SELECT coalesce(max(rowid),0) FROM incoming.fold_history').fetchone()[0]

        report('update_search_bodies')
        db.execute('CREATE TEMP TABLE merge_new_body(id INTEGER PRIMARY KEY)')
        db.execute('''INSERT INTO merge_new_body SELECT p.new_id FROM incoming.body_content b JOIN unified_payload p ON p.origin=? AND p.old_id=b.id
          LEFT JOIN body_content t ON t.id=p.new_id WHERE t.id IS NULL''',(oid,))
        db.execute('INSERT INTO body_content SELECT id FROM merge_new_body')
        db.execute('''INSERT INTO body_fts(rowid,text) SELECT n.id,b.text FROM merge_new_body n JOIN body_text b ON b.id=n.id''')
        db.execute('DROP TABLE merge_new_body')

        report('reconcile_current_heads')
        db.execute('CREATE TEMP TABLE merge_heads AS SELECT h.*,v.new_id AS mapped_version,t.fingerprint AS old_fingerprint,t.published_id AS old_published FROM incoming.message_head h JOIN unified_version v ON v.origin=? AND v.old_id=h.version_id LEFT JOIN message_head t ON t.source_id=h.source_id AND t.source_table=h.source_table AND t.row_key=h.row_key WHERE t.version_id IS NULL OR t.version_id!=v.new_id OR t.fingerprint!=h.fingerprint',(oid,))
        changed=db.execute('SELECT count(*) FROM merge_heads').fetchone()[0]
        db.execute('CREATE TEMP TABLE IF NOT EXISTS next_dirty_fingerprints(digest BLOB PRIMARY KEY) WITHOUT ROWID');db.execute('DELETE FROM next_dirty_fingerprints')
        db.execute('CREATE TEMP TABLE IF NOT EXISTS next_dirty_published(id INTEGER PRIMARY KEY)');db.execute('DELETE FROM next_dirty_published')
        db.execute('INSERT OR IGNORE INTO next_dirty_published SELECT old_published FROM merge_heads WHERE old_published IS NOT NULL')
        db.execute('INSERT OR IGNORE INTO next_dirty_published SELECT published_id FROM merge_heads')
        db.execute('INSERT OR IGNORE INTO next_dirty_fingerprints SELECT old_fingerprint FROM merge_heads WHERE old_fingerprint IS NOT NULL')
        db.execute('''INSERT INTO message_head SELECT source_id,source_table,row_key,account_key,mapped_version,fingerprint,logical_id,published_id,representative FROM merge_heads WHERE 1
          ON CONFLICT(source_id,source_table,row_key) DO UPDATE SET version_id=excluded.version_id,fingerprint=excluded.fingerprint''')
        db.execute('''INSERT OR IGNORE INTO next_dirty_fingerprints SELECT n.fingerprint FROM merge_heads n
          JOIN message_head h ON h.account_key=n.account_key AND h.fingerprint=n.fingerprint AND h.source_id!=n.source_id''')
        db.execute('''INSERT INTO row_head SELECT h.source_id,h.source_table,h.row_key,v.new_id,h.row_hash
          FROM incoming.row_head h JOIN unified_version v ON v.origin=? AND v.old_id=h.version_id WHERE 1
          ON CONFLICT(source_id,source_table,row_key) DO UPDATE SET version_id=excluded.version_id,row_hash=excluded.row_hash
          WHERE row_head.version_id!=excluded.version_id OR row_head.row_hash!=excluded.row_hash''',(oid,))
        for row in sources:
            if row['latest_capture'] is not None:
                db.execute('UPDATE source SET latest_capture=(SELECT new_id FROM unified_capture WHERE origin=? AND old_id=?) WHERE id=?',(oid,row['latest_capture'],row['id']))

        report('link_unique_cross_source_evidence')
        # A single origin may cover more than one account; do not mix accounts.
        for account in sorted({r['account_key'] for r in sources}):
            latest=db.execute('SELECT max(c.id) FROM capture c JOIN source s ON s.id=c.source_id WHERE s.account_key=?',(account,)).fetchone()[0]
            _regroup_bulk(catalog,account,latest)
        report('refresh_unified_projection')
        if origin['raw_cursor']==0:
            for account in sorted({r['account_key'] for r in sources}):catalog._sync_projection(account=account)
        elif changed:catalog._sync_projection(dirty=True)
        db.execute('DROP TABLE merge_heads')
        db.execute('UPDATE unified_origin SET pool_cursor=?,raw_cursor=?,capture_cursor=?,fold_cursor=? WHERE id=?',(maxima['pool'],maxima['raw'],maxima['capture'],fold_max,oid))
        db.execute('INSERT INTO unified_feed(origin,snapshot_sha256,pool_max,raw_max,capture_max,heads_changed,finished) VALUES(?,?,?,?,?,?,?)',(oid,snapshot_sha256,maxima['pool'],maxima['raw'],maxima['capture'],changed,time.time()))
        db.commit()
        return {'origin':origin_state_id,'heads_changed':changed,'pool_max':maxima['pool'],'raw_max':maxima['raw'],'capture_max':maxima['capture'],'no_op':False,'seconds':round(time.monotonic()-started,3)}
    except BaseException:
        db.rollback();raise
    finally:
        db.set_progress_handler(None,0);db.execute('PRAGMA cache_size='+str(previous_cache))
        # Closing statements before detaching also permits retry after rollback.
        for name in ('merge_heads','merge_new_payload','merge_new_body','merge_relink','merge_groups'):
            db.execute('DROP TABLE IF EXISTS temp.'+name)
        db.commit();db.execute('DETACH DATABASE incoming');db.execute('PRAGMA shrink_memory')

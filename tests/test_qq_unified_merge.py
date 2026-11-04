from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
import uuid
from test_qq_next_catalog import fixture
from qq_archive_next.catalog import Catalog,canonical
from qq_archive_next.catalog_merge import merge
from qq_archive_next.history import sha_file


class UnifiedMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.input=self.root/'input.sqlite';fixture(self.input)
        self.source=Catalog(self.root/'source.sqlite');self.target=Catalog(self.root/'target.sqlite')
        self.origin=str(uuid.uuid4());self.ordinal=0
        self.source.import_sqlite(self.input,source_id='current',account_key='account',binding='native-generation',kind='publication')
        self.target.import_sqlite(self.input,source_id='legacy',account_key='account',binding='legacy-digest',kind='publication')
    def tearDown(self):self.source.close();self.target.close();self.temp.cleanup()
    def snapshot(self):
        self.ordinal+=1;path=self.root/f'snapshot-{self.ordinal}.sqlite';copy=sqlite3.connect(path)
        try:self.source.db.backup(copy)
        finally:copy.close()
        return path
    def apply(self,path=None):
        path=path or self.snapshot()
        return merge(self.target,path,origin_state_id=self.origin,snapshot_sha256=sha_file(path)[1])
    def edit(self,text,backfill=False):
        db=sqlite3.connect(self.input);db.execute('UPDATE c2c_messages SET text=? WHERE msg_id=1',(text,))
        if backfill:db.execute("INSERT INTO c2c_messages SELECT -100,1,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,'old backfill',content FROM c2c_messages WHERE msg_id=1")
        db.commit();db.close()
        self.source.import_sqlite(self.input,source_id='current',account_key='account',binding='native-generation',kind='publication')
    def test_all_revisions_unknown_fields_strict_fold_and_exact_noop(self):
        snap=self.snapshot();self.apply(snap)
        self.assertEqual(self.target.summary()['logical_messages'],1)
        self.assertEqual(self.target.summary()['raw_version'],4)
        before=self.target.summary();self.assertTrue(self.apply(snap)['no_op']);self.assertEqual(before,self.target.summary())
        self.edit('edited',True);self.apply()
        self.assertEqual(self.target.summary()['logical_messages'],3)
        raw_count=self.target.summary()['raw_version']
        self.edit('研究计划');self.apply()
        self.assertEqual(self.target.summary()['raw_version'],raw_count)
        self.assertEqual(self.target.summary()['logical_messages'],2)
        self.assertEqual(self.target.db.execute('SELECT count(*) FROM unified_capture').fetchone()[0],self.source.summary()['capture'])
        self.target.verify()
    def test_recipe_reuse_preserves_existing_representation_and_new_atom_ids(self):
        same=canonical({'leaf':'x'*1000})
        self.target.pool.intern_text(same);self.target.db.commit()
        old=self.source.pool.intern_json(same)
        other=canonical({'leaf':'y'*1200,'other':'z'*1300});new=self.source.pool.intern_json(other);self.source.db.commit()
        self.apply()
        mapped={v[0]:v[1] for v in self.target.db.execute('SELECT old_id,new_id FROM unified_payload')}
        self.assertEqual(self.target.pool.get_text(mapped[old]),same)
        self.assertEqual(self.target.pool.get_text(mapped[new]),other)
        self.target.verify()
    def test_failure_rolls_back_merge_and_retry_is_safe(self):
        before=self.target.summary()
        with self.assertRaises(Exception):merge(self.target,self.snapshot(),origin_state_id=self.origin,snapshot_sha256='a'*64,cancelled=lambda:True)
        self.assertEqual(before,self.target.summary())
        self.apply();self.target.verify()

    def test_bulk_folding_preserves_ambiguity_and_account_boundaries(self):
        db=sqlite3.connect(self.input)
        db.execute('INSERT INTO c2c_messages SELECT 2,timestamp,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,text,content FROM c2c_messages WHERE msg_id=1')
        db.commit();db.close()
        self.source.import_sqlite(self.input,source_id='current',account_key='account',binding='native-generation',kind='publication')
        self.source.import_sqlite(self.input,source_id='other-account',account_key='second-account',binding='separate',kind='publication')
        self.apply()
        self.assertEqual(self.target.db.execute("SELECT count(*) FROM current_message WHERE account_key='account'").fetchone()[0],3)
        self.assertEqual(self.target.db.execute("SELECT count(*) FROM current_message WHERE account_key='second-account'").fetchone()[0],2)
        self.assertFalse(self.target.db.execute("SELECT 1 FROM message_head WHERE logical_id LIKE 'strict_%'").fetchone())
        self.target.verify()

from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path

from qq_archive_next.catalog import Catalog
from qq_archive_next.codec import encode,decode


def fixture(path):
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE c2c_messages(msg_id INTEGER PRIMARY KEY,timestamp INTEGER,direction INTEGER,sender_uid TEXT,sender_qq INTEGER,peer_uid TEXT,peer_qq INTEGER,msg_type INTEGER,content_type INTEGER,proto_ver TEXT,inner_ts INTEGER,text TEXT,content TEXT)')
    db.execute('CREATE TABLE unknown_fields(id INTEGER PRIMARY KEY,opaque BLOB,nullable TEXT,number REAL)')
    db.execute('INSERT INTO c2c_messages VALUES(1,100,0,\'sender\',123,\'peer\',456,1,1,NULL,NULL,\'研究计划\',\'{}\')')
    db.execute('INSERT INTO unknown_fields VALUES(1,?,NULL,-0.5)',(bytes(range(256)),))
    db.commit();db.close()


class NextCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.source=self.root/'source.sqlite';fixture(self.source)
        self.catalog=Catalog(self.root/'state/catalog.sqlite')
    def tearDown(self):self.catalog.close();self.temporary.cleanup()
    def ingest(self,source='a',path=None,**kwargs):
        return self.catalog.import_sqlite(path or self.source,source_id=source,account_key='account',binding='fixed-'+source,kind='publication',**kwargs)
    def change(self,statement,values=()):
        db=sqlite3.connect(self.source);db.execute(statement,values);db.commit();db.close()

    def test_exact_unknown_fields_and_noop_zero_growth(self):
        first=self.ingest();self.assertEqual(first['versions_added'],2)
        before=self.catalog.summary()
        self.assertTrue(self.ingest()['no_op'])
        self.assertEqual(before,self.catalog.summary())
        self.assertEqual(self.catalog.verify()['raw_versions_verified'],2)
        raw=self.catalog.db.execute("SELECT payload_id FROM raw_version WHERE source_table='unknown_fields'").fetchone()[0]
        self.assertEqual(self.catalog.raw_values(raw),(1,bytes(range(256)),None,-0.5))

    def test_backfill_old_id_edit_and_return_to_old_version(self):
        self.ingest()
        self.change("INSERT INTO c2c_messages SELECT -10,1,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,'早期回填',content FROM c2c_messages WHERE msg_id=1")
        self.change("UPDATE c2c_messages SET text='编辑版本' WHERE msg_id=1")
        self.assertEqual(self.ingest()['message_versions_added'],2)
        self.change("UPDATE c2c_messages SET text='研究计划' WHERE msg_id=1")
        report=self.ingest()
        self.assertFalse(report['no_op']);self.assertEqual(report['versions_added'],0)
        self.assertEqual(self.catalog.summary()['message_version'],3)
        self.assertEqual(self.catalog.summary()['logical_messages'],2)

    def test_failure_rolls_back_pool_heads_and_receipt(self):
        self.ingest();before=self.catalog.summary()
        self.change("UPDATE c2c_messages SET text='新内容' WHERE msg_id=1")
        def fail(stage):raise RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):self.ingest(fault=fail)
        self.assertEqual(before,self.catalog.summary())
        self.assertEqual(self.ingest()['message_versions_added'],1)

    def test_cross_source_strict_fold_becomes_ambiguous_without_losing_observations(self):
        self.ingest('a');self.ingest('b')
        self.assertEqual(self.catalog.summary()['logical_messages'],1)
        other=self.root/'other.sqlite';fixture(other)
        db=sqlite3.connect(other)
        db.execute('INSERT INTO c2c_messages SELECT 2,timestamp,direction,sender_uid,sender_qq,peer_uid,peer_qq,msg_type,content_type,proto_ver,inner_ts,text,content FROM c2c_messages')
        db.commit();db.close()
        self.ingest('b',other)
        self.assertEqual(self.catalog.summary()['logical_messages'],3)
        self.assertEqual(self.catalog.summary()['message_version'],3)
        self.catalog.verify()

    def test_missing_source_row_is_retained_and_schema_drift_rejected(self):
        self.ingest();self.change('DELETE FROM c2c_messages')
        self.assertTrue(self.ingest()['no_op'])
        self.assertEqual(self.catalog.summary()['logical_messages'],1)
        self.change('ALTER TABLE unknown_fields ADD COLUMN new_value TEXT')
        with self.assertRaisesRegex(ValueError,'schema drift'):self.ingest()

    def test_typed_values_do_not_collapse(self):
        values=(None,b'1','1',1,-2**63,2**63-1,-0.0,0.0)
        self.assertEqual(encode(decode(encode(values))),encode(values))
        self.assertNotEqual(encode((-0.0,)),encode((0.0,)))
        from qq_archive_next.codec import encode_binary,decode_binary
        self.assertEqual(encode(decode_binary(encode_binary(values))),encode(values))

    def test_nullable_primary_keys_keep_distinct_native_rows(self):
        self.change('CREATE TABLE nullable_keys(k TEXT PRIMARY KEY,value TEXT)')
        self.change("INSERT INTO nullable_keys VALUES(NULL,'first')")
        self.change("INSERT INTO nullable_keys VALUES(NULL,'second')")
        self.ingest()
        self.assertEqual(self.catalog.db.execute("SELECT count(*) FROM row_head WHERE source_table='nullable_keys'").fetchone()[0],2)


if __name__=='__main__':unittest.main()

from pathlib import Path
import sqlite3
import tempfile
import unittest

from qq_next_fixture import native
from qq_archive_next.row_locator import locate, roots
from qq_archive_next.native_sqlite import readonly
from qq_archive_next.catalog import Catalog


class ChangedRowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=self.root/'native.sqlite';native(self.path)
        db=sqlite3.connect(self.path)
        db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,value BLOB)')
        db.executemany('INSERT INTO records VALUES(?,?)',((n,b'x'*160) for n in range(-1500,1500)))
        db.execute('CREATE TABLE keyed(k TEXT PRIMARY KEY,value BLOB) WITHOUT ROWID')
        db.execute("INSERT INTO keyed VALUES('a',x'01')")
        db.commit();db.close()
        self.before=self.path.read_bytes()
        con=readonly(self.path)
        try:self.roots=roots(con)
        finally:con.close()

    def tearDown(self):self.temp.cleanup()

    def change(self,sql,parameters=()):
        con=sqlite3.connect(self.path);con.execute(sql,parameters);con.commit();con.close()

    def windows(self):
        after=self.path.read_bytes()
        changed=[n//4096+1 for n in range(0,len(after),4096) if self.before[n:n+4096]!=after[n:n+4096]]
        con=readonly(self.path)
        try:return locate(con,self.path,changed,self.roots)[0]
        finally:con.close()

    def test_negative_64bit_row_and_changed_leaf(self):
        self.change('INSERT INTO records VALUES(?,?)',(-2**63,b'old backfill'))
        self.change('UPDATE records SET value=? WHERE id=-1200',(b'edited',))
        window=self.windows()['records']
        if window is not None:
            self.assertIn(-2**63,window);self.assertIn(-1200,window)
            self.assertLess(len(window),3001)

    def test_overflow_only_change_finds_owning_row(self):
        self.change('UPDATE evidence SET value=? WHERE id=1',(b'a'*8192,))
        self.before=self.path.read_bytes()
        self.change('UPDATE evidence SET value=? WHERE id=1',(b'a'*5000+b'b'+b'a'*3191,))
        window=self.windows()['evidence']
        self.assertIn(1,window)

    def test_secondary_index_churn_does_not_force_full_message_scan(self):
        self.change('CREATE INDEX record_value ON records(value)')
        self.before=self.path.read_bytes()
        con=readonly(self.path)
        try:self.roots=roots(con)
        finally:con.close()
        self.change('UPDATE records SET value=? WHERE id=-1200',(b'changed indexed value',))
        window=self.windows()['records']
        self.assertIsNotNone(window);self.assertIn(-1200,window);self.assertLess(len(window),3000)

    def test_full_and_changed_row_imports_have_identical_evidence(self):
        catalogs=[Catalog(self.root/name/'catalog.sqlite') for name in ('full','incremental')]
        try:
            args=dict(source_id='test',account_key='account',binding='fixed',kind='publication')
            for catalog in catalogs:catalog.import_sqlite(self.path,**args)
            self.change('UPDATE records SET value=? WHERE id=42',(bytes(range(256))*20,))
            self.change('INSERT INTO records VALUES(?,?)',(-2**63,b'late'))
            self.change("UPDATE keyed SET value=x'0203' WHERE k='a'")
            self.change('CREATE INDEX record_value ON records(value)')
            window=self.windows()
            self.assertIsNone(window['keyed'])
            catalogs[0].import_sqlite(self.path,**args)
            catalogs[1].import_sqlite(self.path,row_windows=window,**args)
            def evidence(catalog):
                return list(catalog.db.execute('SELECT source_table,hex(row_key),hex(row_hash) FROM raw_version ORDER BY 1,2,3'))
            self.assertEqual([tuple(r) for r in evidence(catalogs[0])],[tuple(r) for r in evidence(catalogs[1])])
            for catalog in catalogs:catalog.verify()
        finally:
            for catalog in catalogs:catalog.close()

    def test_split_and_new_root_use_conservative_fallback(self):
        self.change('CREATE TABLE new_rows(id INTEGER PRIMARY KEY,value BLOB)')
        con=sqlite3.connect(self.path)
        con.executemany('INSERT INTO records VALUES(?,?)',((n,b'y'*200) for n in range(1500,12000)))
        con.commit();con.close()
        window=self.windows()
        self.assertIsNone(window['new_rows'])
        if window['records'] is not None:self.assertTrue(set(range(1500,12000)).issubset(window['records']))


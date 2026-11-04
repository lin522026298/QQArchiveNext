from __future__ import annotations
import sqlite3,tempfile,unittest
from pathlib import Path
from qq_archive_next.native_sqlite import readonly,runtime_directory
from qq_archive_next.codec import encode


@unittest.skipUnless((runtime_directory()/'runtime.json').is_file(),'private NTQQ SQLite runtime not built')
class NativeReaderTests(unittest.TestCase):
    def test_native_values_match_normal_sqlite_and_input_is_readonly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'input.sqlite'
            db=sqlite3.connect(path);db.execute('CREATE TABLE t(a,b,c,d,e,f)')
            db.execute('INSERT INTO t VALUES(?,?,?,?,?,?)',(-2**63,2**63-1,'中文\0文字',bytes(range(256)),None,-0.5))
            db.commit();expected=db.execute('SELECT * FROM t').fetchone();db.close()
            before=path.read_bytes();normal_version=sqlite3.sqlite_version
            native=readonly(path)
            self.assertEqual(encode(tuple(native.execute('SELECT * FROM t').fetchone())),encode(expected))
            self.assertEqual(native.execute('SELECT b FROM t WHERE a=?',(-2**63,)).fetchone()['b'],2**63-1)
            with self.assertRaises(sqlite3.OperationalError):native.execute('INSERT INTO t(a) VALUES(1)').fetchall()
            self.assertEqual(native.execute('PRAGMA quick_check').fetchone()[0],'ok')
            native.close();self.assertEqual(before,path.read_bytes())
            self.assertEqual(normal_version,sqlite3.sqlite_version)
    def test_dbstat_mapping_can_use_memory_temp_without_writing_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'input.sqlite'
            db=sqlite3.connect(path);db.execute('CREATE TABLE t(a PRIMARY KEY,b)');db.execute("INSERT INTO t VALUES(1,'value')");db.commit();db.close()
            before=path.read_bytes();native=readonly(path)
            native.execute('CREATE TEMP TABLE changed(number INTEGER PRIMARY KEY)')
            native.executemany('INSERT INTO changed VALUES(?)',[(2,)])
            self.assertIn('t',[r[0] for r in native.execute('SELECT name FROM dbstat WHERE pageno IN (SELECT number FROM changed)')])
            native.close();self.assertEqual(before,path.read_bytes())


if __name__=='__main__':unittest.main()

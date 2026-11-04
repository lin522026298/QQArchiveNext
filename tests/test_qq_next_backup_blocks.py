from pathlib import Path
import random
import sqlite3
import struct
import tempfile
import unittest

from qq_archive_next.backup_blocks import Pack
from qq_archive_next.backup import repository,store_file


class BlockPackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.repo=repository(self.root/'repo',create=True)
        self.pack=Pack(self.repo)
    def tearDown(self):self.pack.close();self.temp.cleanup()

    def test_multi_level_tree_order_empty_file_and_reference_hash(self):
        path=self.root/'pages.sqlite'
        with path.open('wb') as stream:
            for n in range(4097):stream.write(struct.pack('<I',n)*1024)
        entry=self.pack.file(path,block_size=4096);self.pack.commit()
        self.assertEqual(b''.join(self.pack.data(entry['root'])),path.read_bytes())
        root=dict(entry['root']);root['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'reference differs'):self.pack.get(root)
        empty=self.root/'empty';empty.touch();entry=self.pack.file(empty,block_size=4096)
        self.assertEqual(b''.join(self.pack.data(entry['root'])),b'')

    def test_small_sqlite_edit_does_not_rewrite_megabyte_containers(self):
        path=self.root/'random.sqlite';db=sqlite3.connect(path);db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,payload BLOB)')
        generator=random.Random(20261103)
        db.executemany('INSERT INTO evidence VALUES(?,?)',((n,generator.randbytes(256*1024)) for n in range(32)))
        db.commit();db.close()
        first=self.pack.file(path,block_size=4096);self.pack.commit();before=self.pack.new_bytes
        db=sqlite3.connect(path);value=bytearray(db.execute('SELECT payload FROM evidence WHERE id=31').fetchone()[0]);value[-1]^=1
        db.execute('UPDATE evidence SET payload=? WHERE id=31',(bytes(value),));db.commit();db.close()
        second=self.pack.file(path,block_size=4096,previous=first);self.pack.commit()
        growth=self.pack.new_bytes-before
        self.assertNotEqual(first['root'],second['root']);self.assertLess(growth,128*1024)
        self.assertEqual(b''.join(self.pack.data(second['root'])),path.read_bytes())
        before=self.pack.new_bytes;third=self.pack.file(path,block_size=4096,previous=second)
        self.assertEqual(second,third);self.assertEqual(self.pack.new_bytes,before)

    def test_prior_tree_handles_growing_shrinking_and_empty_files(self):
        path=self.root/'changing.sqlite';previous=None
        for data in (b'a'*4096*130,b'a'*4096*130+b'b'*5000,b'a'*4096*12+b'c',b'',b'd'*4097):
            path.write_bytes(data);entry=self.pack.file(path,block_size=4096,previous=previous)
            self.pack.commit();self.assertEqual(b''.join(self.pack.data(entry['root'])),data)
            previous=entry

    def test_legacy_slices_reuse_existing_bytes_and_trie_reuses_unchanged_buckets(self):
        path=self.root/'pages.sqlite';path.write_bytes(b''.join(struct.pack('<I',n)*1024 for n in range(130)))
        old,_=store_file(self.repo,path,lambda:False)
        seeded=self.pack.seed_legacy({'id':'a'*64,'body':{'files':{'catalog.sqlite':old}}})
        self.assertGreater(seeded,0);self.assertEqual(self.pack.new_bytes,52*seeded)
        entry=self.pack.file(path,block_size=4096);self.assertEqual(b''.join(self.pack.data(entry['root'])),path.read_bytes())
        files={f'objects/{n:064x}':entry for n in range(200)}
        first=self.pack.file_tree(files);self.pack.commit();before=self.pack.new_bytes
        self.assertEqual(dict(self.pack.files(first)),files)
        files['config.private.json']=entry;second=self.pack.file_tree(files,previous=first);self.pack.commit()
        self.assertEqual(dict(self.pack.files(second)),files)
        self.assertLess(self.pack.new_bytes-before,64*1024)
        self.assertEqual(self.pack.file_tree(files,previous=second),second)


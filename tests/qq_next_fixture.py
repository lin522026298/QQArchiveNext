"""Synthetic SQLite-generated pages and WAL, encrypted with the NTQQ binding."""
from pathlib import Path
import hashlib,hmac,sqlite3,struct
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from qq_archive_next.cipher import PAGE,PageCipher,WRAPPER
from qq_archive_next.wal import checksum,STRIDE

PASSWORD=b'0123456789abcdef'
SALT=bytes(range(16))


def native(path:Path):
    db=sqlite3.connect(path);db.execute('PRAGMA page_size=4096');db.execute('VACUUM');db.close()
    data=bytearray(path.read_bytes());data[20]=48;data[105:107]=struct.pack('>H',4048)
    path.write_bytes(data)
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,value BLOB)')
    db.execute('INSERT INTO evidence VALUES(1,?)',(bytes(range(256))*32,))
    db.commit()
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    db.close()


def encrypt_page(plain,number,password=PASSWORD):
    cipher=PageCipher(password,SALT)
    start=16 if number==1 else 0
    iv=hashlib.sha256(struct.pack('<I',number)+plain).digest()[:16]
    encryptor=Cipher(algorithms.AES(cipher.key),modes.CBC(iv)).encryptor()
    body=encryptor.update(plain[start:-48])+encryptor.finalize()
    mac=hmac.new(cipher.mac_key,body+iv+struct.pack('<I',number),'sha1').digest()
    return (SALT if number==1 else b'')+body+iv+mac+bytes(12)


def wrap(plain:Path,destination:Path):
    data=plain.read_bytes()
    destination.write_bytes(b'SYNTHETIC NTQQ'+bytes(WRAPPER-14)+b''.join(encrypt_page(data[n:n+PAGE],n//PAGE+1) for n in range(0,len(data),PAGE)))


def encrypted_wal(native_wal:bytes):
    header=native_wal[:32];endian='<' if struct.unpack('>I',header[:4])[0]==0x377f0682 else '>'
    first,second=struct.unpack('>II',header[24:32]);result=bytearray(header)
    for n in range(32,len(native_wal),STRIDE):
        frame=native_wal[n:n+STRIDE]
        if len(frame)!=STRIDE:break
        page=encrypt_page(frame[24:],struct.unpack('>I',frame[:4])[0])
        first,second=checksum(frame[:8]+page,endian,first,second)
        result+=frame[:16]+struct.pack('>II',first,second)+page
    return bytes(result)

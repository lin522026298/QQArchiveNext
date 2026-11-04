"""NTQQ 9.9.19 Windows binding, authenticated against existing saved keys.

1024-byte wrapper; 4096-byte SQLCipher pages; PBKDF2-HMAC-SHA512/4000;
AES-256-CBC; 48 reserved bytes; HMAC-SHA1; little-endian page numbers.
The source and its keys are never opened for writing.
"""
from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE, WRAPPER, RESERVE = 4096, 1024, 48
BINDING = "ntqq-windows-9.9.19-sqlcipher-sha512-sha1-4000-v1"


class AuthenticationError(ValueError):
    pass


class PageCipher:
    def __init__(self, password: bytes, salt: bytes):
        if len(salt) != 16 or len(password) != 16:
            raise AuthenticationError("unsupported NTQQ key/salt shape")
        self.salt = salt
        self.key = hashlib.pbkdf2_hmac('sha512', password, salt, 4000, 32)
        self.mac_key = hashlib.pbkdf2_hmac('sha512', self.key, bytes(x ^ 0x3a for x in salt), 2, 32)

    def decrypt(self, page: bytes, number: int) -> bytes:
        if len(page) != PAGE or not 1 <= number <= 0xffffffff:
            raise AuthenticationError("invalid encrypted page shape")
        offset = 16 if number == 1 else 0
        if number == 1 and page[:16] != self.salt:
            raise AuthenticationError("database salt changed")
        expected = hmac.new(self.mac_key, page[offset:-32] + struct.pack('<I', number), 'sha1').digest()
        if not hmac.compare_digest(expected, page[-32:-12]):
            raise AuthenticationError("NTQQ page authentication failed")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(page[-48:-32])).decryptor()
        body = decryptor.update(page[offset:-48]) + decryptor.finalize()
        prefix = b'SQLite format 3\0' if number == 1 else b''
        return prefix + body + bytes(RESERVE)

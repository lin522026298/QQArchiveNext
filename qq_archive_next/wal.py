"""Validate every frame and freeze the last complete committed WAL prefix."""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .cipher import PAGE, AuthenticationError

STRIDE = PAGE + 24
WAL_SPOOL_BYTES = 8 * 1024**2


def checksum(data: bytes, endian: str, first=0, second=0):
    if len(data) % 8:
        raise AuthenticationError('unaligned WAL checksum input')
    words = struct.unpack(endian + str(len(data)//4) + 'I', data)
    for i in range(0, len(words), 2):
        first = (first + words[i] + second) & 0xffffffff
        second = (second + words[i+1] + first) & 0xffffffff
    return first, second


@dataclass
class FrozenWal:
    stream: object
    header: bytes
    committed_frames: int
    database_pages: int | None
    prefix_bytes: int
    prefix_hash: str
    total_frames: int

    def pages(self):
        self.stream.seek(32)
        for _ in range(self.committed_frames):
            header = self.stream.read(24)
            yield struct.unpack('>I', header[:4])[0], self.stream.read(PAGE)

    def close(self):
        self.stream.close()


def freeze(path: Path, *, memory_bytes=WAL_SPOOL_BYTES, directory=None, maximum_bytes=None, cancelled=lambda:False) -> FrozenWal:
    # SpooledTemporaryFile spills rather than imposing an input-size ceiling.
    stream = tempfile.SpooledTemporaryFile(max_size=memory_bytes, mode='w+b', dir=directory)
    try:
        if not path.exists():
            return FrozenWal(stream, b'', 0, None, 0, hashlib.sha256(b'').hexdigest(), 0)
        with path.open('rb') as source:
            # A reader must finish even if the writer keeps appending frames.
            size_at_open=os.fstat(source.fileno()).st_size
            if maximum_bytes is not None and size_at_open>maximum_bytes:
                raise AuthenticationError('WAL too large for guarded refresh during capture')
            frame_budget = max(0, (size_at_open-32)//STRIDE)
            header = source.read(32)
            if not header:
                return FrozenWal(stream, b'', 0, None, 0, hashlib.sha256(b'').hexdigest(), 0)
            if len(header) != 32:
                raise AuthenticationError('incomplete WAL header')
            magic, version, size = struct.unpack('>III', header[:12])
            if magic not in (0x377f0682,0x377f0683) or version != 3007000 or size != PAGE:
                raise AuthenticationError('unsupported WAL header')
            endian = '<' if magic == 0x377f0682 else '>'
            running = checksum(header[:24], endian)
            if running != struct.unpack('>II', header[24:]):
                raise AuthenticationError('invalid WAL header checksum')
            stream.write(header)
            digest = hashlib.sha256(header)
            committed = total = 0
            database_pages = None
            committed_hash = digest.hexdigest()
            for _ in range(frame_budget):
                if cancelled():raise InterruptedError('WAL freeze cancelled')
                frame = source.read(STRIDE)
                if not frame:
                    break
                if len(frame) != STRIDE:
                    break  # A concurrent writer may leave an incomplete tail.
                number, pages = struct.unpack('>II', frame[:8])
                if frame[8:16] != header[16:24]:
                    break  # SQLite may retain frames from the previous WAL epoch.
                checked = checksum(frame[:8] + frame[24:], endian, *running)
                if checked != struct.unpack('>II', frame[16:24]):
                    # A same-epoch complete frame with a bad checksum cannot be
                    # advertised as an up-to-date capture. Concurrent writes retry.
                    raise AuthenticationError('invalid complete WAL frame checksum; retry a stable cut')
                if not number:
                    raise AuthenticationError('zero WAL page number')
                running = checked
                total += 1
                stream.write(frame)
                digest.update(frame)
                if pages:
                    committed, database_pages, committed_hash = total, pages, digest.hexdigest()
            prefix_bytes = 32 + committed * STRIDE
            stream.truncate(prefix_bytes)
            return FrozenWal(stream, header, committed, database_pages, prefix_bytes, committed_hash, total)
    except BaseException:
        stream.close()
        raise


def epoch(path: Path):
    """Identity and full header of the currently visible WAL generation."""
    try:
        with path.open('rb', buffering=0) as source:
            stat = os.fstat(source.fileno())
            header = source.read(32)
    except FileNotFoundError:
        return None
    if not header:
        return None
    if len(header) != 32 or not stat.st_ino:
        raise AuthenticationError('incomplete WAL identity during capture')
    return (stat.st_dev, stat.st_ino, header)


def verify_epoch(path: Path, expected, *, commit=None):
    """Check the epoch and an optional commit witness in constant-size reads."""
    try:
        with path.open('rb', buffering=0) as source:
            stat = os.fstat(source.fileno())
            if (stat.st_dev, stat.st_ino, source.read(32)) != expected:
                raise AuthenticationError('WAL reset during capture')
            if commit is not None:
                offset, header = commit
                source.seek(offset)
                if len(header) != 24 or source.read(24) != header:
                    raise AuthenticationError('WAL commit changed during capture')
            # A reset can race a read even when the first header matched.
            source.seek(0)
            if source.read(32) != expected[2]:
                raise AuthenticationError('WAL reset during capture')
        current = path.stat()
    except FileNotFoundError:
        raise AuthenticationError('WAL disappeared during capture') from None
    if (current.st_dev, current.st_ino) != expected[:2]:
        raise AuthenticationError('WAL replaced during capture')


def verify_live_prefix(path: Path, frozen: FrozenWal, *, cancelled=lambda:False) -> None:
    if not frozen.header:
        if path.exists() and path.stat().st_size:
            # A newly created WAL is safe only after a new capture attempt.
            raise AuthenticationError('WAL appeared during capture')
        return
    digest = hashlib.sha256()
    remaining = frozen.prefix_bytes
    with path.open('rb') as source:
        while remaining:
            if cancelled():raise InterruptedError('WAL verification cancelled')
            block = source.read(min(1024**2, remaining))
            if not block:
                raise AuthenticationError('WAL reset during capture')
            digest.update(block)
            remaining -= len(block)
    if digest.hexdigest() != frozen.prefix_hash:
        raise AuthenticationError('WAL prefix changed during capture')

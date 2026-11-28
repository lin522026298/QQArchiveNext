"""Pin SQLite's Windows READ_LOCK(0) without writing source or shm bytes.

The standard Win32 WAL protocol uses byte 123 of the existing -shm file.
A shared read0 lock prevents checkpoint writes to the main database, while WAL
writers use a different byte. Optimistic main/WAL validation is still required.
No shm is created. Other platforms/missing shm retain the optimistic algorithm.
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as W
import os
from pathlib import Path
import time
from .cipher import AuthenticationError


def shared_memory_identity(source):
    if os.name != 'nt':
        return None
    try:
        stat=Path(str(source)+'-shm').stat()
    except FileNotFoundError:
        return None
    return stat.st_dev,stat.st_ino


class CheckpointGuard:
    def __init__(self,source):
        self.path=Path(str(source)+'-shm');self.handle=None;self.mode='optimistic_no_shm'
        self.started=time.monotonic();self.seconds=0.0;self.identity=None
        if os.name!='nt':self.mode='optimistic_platform';return
        expected=shared_memory_identity(source)
        class Overlapped(ctypes.Structure):
            _fields_=[('internal',ctypes.c_size_t),('internal_high',ctypes.c_size_t),
                      ('offset',W.DWORD),('offset_high',W.DWORD),('event',W.HANDLE)]
        lib=ctypes.WinDLL('kernel32',use_last_error=True)
        lib.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.DWORD,W.HANDLE];lib.CreateFileW.restype=W.HANDLE
        lib.LockFileEx.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,W.DWORD,ctypes.POINTER(Overlapped)];lib.LockFileEx.restype=W.BOOL
        lib.UnlockFileEx.argtypes=lib.LockFileEx.argtypes[:1]+[W.DWORD,W.DWORD,W.DWORD,ctypes.POINTER(Overlapped)];lib.UnlockFileEx.restype=W.BOOL
        lib.CloseHandle.argtypes=[W.HANDLE];lib.CloseHandle.restype=W.BOOL
        handle=lib.CreateFileW(str(self.path),0x80000000,7,None,3,0,None)
        if handle==ctypes.c_void_p(-1).value:
            error=ctypes.get_last_error()
            if error in (2,3):return
            raise AuthenticationError('cannot open existing source shm for a read-only checkpoint guard')
        self.lib=lib;self.overlap=Overlapped();self.overlap.offset=123
        # FAIL_IMMEDIATELY, shared (no LOCKFILE_EXCLUSIVE_LOCK).
        if not lib.LockFileEx(handle,1,0,1,0,ctypes.byref(self.overlap)):
            lib.CloseHandle(handle)
            raise AuthenticationError('source checkpoint is active; retry a read-only cut')
        self.handle=handle;self.mode='win32_sqlite_read0'
        try:
            stat=self.path.stat();self.identity=(stat.st_dev,stat.st_ino)
            if self.identity!=expected:
                raise AuthenticationError('source shared-memory generation changed during capture')
        except BaseException:self.release();raise

    def validate(self):
        if self.handle is None:return
        try:stat=self.path.stat()
        except OSError:raise AuthenticationError('source shared-memory generation disappeared') from None
        if (stat.st_dev,stat.st_ino)!=self.identity:raise AuthenticationError('source shared-memory generation changed')
        if time.monotonic()-self.started>30:raise AuthenticationError('read-only input freeze exceeded its checkpoint guard budget')

    def release(self):
        self.seconds=round(time.monotonic()-self.started,3)
        if self.handle is None:return
        handle=self.handle;self.handle=None
        try:self.lib.UnlockFileEx(handle,0,1,0,ctypes.byref(self.overlap))
        finally:self.lib.CloseHandle(handle)

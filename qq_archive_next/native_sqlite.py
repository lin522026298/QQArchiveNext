"""Private read-only SQLite runtime for NTQQ's shifted lock-byte page.

This DLL is a separate SQLite instance, compiled with the NTQQ pending-byte
position. It never changes Python's SQLite runtime or opens an input writable.
No sqlite3_test_control or process memory patching is used.
"""
from __future__ import annotations
import ctypes as C
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

_library=None


def runtime_directory():
    bundled=Path(__file__).resolve().parent/'native'
    if (bundled/'runtime.json').is_file():return bundled
    return Path(__file__).resolve().parents[1]/'work/qq_native_build/runtime'


def library():
    global _library
    if _library is not None:return _library
    root=runtime_directory();metadata=json.loads((root/'runtime.json').read_text(encoding='utf-8'))
    path=root/'sqlite3-ntqq-reader.dll'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['dll_sha256']:
        raise RuntimeError('native QQ reader checksum mismatch')
    dll=C.CDLL(str(path))
    def bind(name,result,args):
        fn=getattr(dll,name);fn.restype=result;fn.argtypes=args;return fn
    bind('sqlite3_libversion',C.c_char_p,[])
    bind('qq_sqlite_pending_byte',C.c_int,[])
    if dll.sqlite3_libversion().decode()!=metadata['sqlite_version'] or dll.qq_sqlite_pending_byte()!=0x3ffffc00:
        raise RuntimeError('unsupported native QQ SQLite binding')
    pointer=C.c_void_p
    bind('sqlite3_open_v2',C.c_int,[C.c_char_p,C.POINTER(pointer),C.c_int,C.c_char_p])
    bind('sqlite3_close_v2',C.c_int,[pointer])
    bind('sqlite3_errmsg',C.c_char_p,[pointer])
    bind('sqlite3_prepare_v2',C.c_int,[pointer,C.c_char_p,C.c_int,C.POINTER(pointer),C.POINTER(C.c_char_p)])
    bind('sqlite3_step',C.c_int,[pointer]);bind('sqlite3_finalize',C.c_int,[pointer])
    bind('sqlite3_column_count',C.c_int,[pointer]);bind('sqlite3_column_name',C.c_char_p,[pointer,C.c_int])
    bind('sqlite3_column_type',C.c_int,[pointer,C.c_int]);bind('sqlite3_column_int64',C.c_int64,[pointer,C.c_int])
    bind('sqlite3_column_double',C.c_double,[pointer,C.c_int]);bind('sqlite3_column_bytes',C.c_int,[pointer,C.c_int])
    bind('sqlite3_column_blob',pointer,[pointer,C.c_int]);bind('sqlite3_column_text',pointer,[pointer,C.c_int])
    bind('sqlite3_bind_null',C.c_int,[pointer,C.c_int]);bind('sqlite3_bind_int64',C.c_int,[pointer,C.c_int,C.c_int64])
    bind('sqlite3_bind_double',C.c_int,[pointer,C.c_int,C.c_double])
    bind('sqlite3_bind_text',C.c_int,[pointer,C.c_int,C.c_char_p,C.c_int,pointer])
    bind('sqlite3_bind_blob',C.c_int,[pointer,C.c_int,pointer,C.c_int,pointer])
    bind('sqlite3_progress_handler',None,[pointer,C.c_int,pointer,pointer])
    _library=dll
    return dll


class Row:
    def __init__(self,names,indices,values):self.names,self.indices,self.values=names,indices,values
    def __getitem__(self,key):return self.values[self.indices[key]] if isinstance(key,str) else self.values[key]
    def __iter__(self):return iter(self.values)
    def __len__(self):return len(self.values)
    def keys(self):return self.names


class Cursor:
    def __init__(self,connection,sql,parameters):
        self.connection=connection;self.lib=connection.lib;self.pointer=C.c_void_p();self.done=False
        tail=C.c_char_p()
        code=self.lib.sqlite3_prepare_v2(connection.pointer,sql.encode('utf-8'),-1,C.byref(self.pointer),C.byref(tail))
        if code:raise sqlite3.OperationalError(connection.error())
        self.names=tuple(self.lib.sqlite3_column_name(self.pointer,n).decode('utf-8') for n in range(self.lib.sqlite3_column_count(self.pointer)))
        self.indices={name:i for i,name in enumerate(self.names)}
        connection.cursors.add(self)
        try:
            for ordinal,value in enumerate(parameters,1):
                if value is None:code=self.lib.sqlite3_bind_null(self.pointer,ordinal)
                elif isinstance(value,int):code=self.lib.sqlite3_bind_int64(self.pointer,ordinal,value)
                elif isinstance(value,float):code=self.lib.sqlite3_bind_double(self.pointer,ordinal,value)
                else:
                    data=value.encode('utf-8') if isinstance(value,str) else bytes(value)
                    if isinstance(value,str):code=self.lib.sqlite3_bind_text(self.pointer,ordinal,data,len(data),C.c_void_p(-1))
                    else:code=self.lib.sqlite3_bind_blob(self.pointer,ordinal,C.c_char_p(data),len(data),C.c_void_p(-1))
                if code:raise sqlite3.OperationalError(connection.error())
            if not self.names:self.fetchone()
        except BaseException:self.close();raise
    def fetchone(self):
        if self.done:return None
        code=self.lib.sqlite3_step(self.pointer)
        if code==101:self.close();return None
        if code!=100:
            error=self.connection.error();self.close();raise sqlite3.OperationalError(error)
        values=[]
        for ordinal in range(len(self.names)):
            kind=self.lib.sqlite3_column_type(self.pointer,ordinal)
            if kind==5:values.append(None)
            elif kind==1:values.append(self.lib.sqlite3_column_int64(self.pointer,ordinal))
            elif kind==2:values.append(self.lib.sqlite3_column_double(self.pointer,ordinal))
            else:
                pointer=(self.lib.sqlite3_column_text if kind==3 else self.lib.sqlite3_column_blob)(self.pointer,ordinal)
                size=self.lib.sqlite3_column_bytes(self.pointer,ordinal)
                data=C.string_at(pointer,size) if size else b''
                values.append(data.decode('utf-8') if kind==3 else data)
        return Row(self.names,self.indices,tuple(values))
    def fetchall(self):return list(self)
    def __iter__(self):
        while True:
            row=self.fetchone()
            if row is None:break
            yield row
    def close(self):
        if not self.done:
            self.lib.sqlite3_finalize(self.pointer);self.done=True
            self.connection.cursors.discard(self)


class NativeConnection:
    def __init__(self,path:Path,*,cancelled=None):
        self.lib=library();self.pointer=C.c_void_p();self.cursors=set();self.row_factory=None;self.callback=None
        uri=Path(path).resolve(strict=True).as_uri()+'?mode=ro&immutable=1'
        code=self.lib.sqlite3_open_v2(uri.encode('utf-8'),C.byref(self.pointer),0x1|0x40,None)
        if code:
            error=self.error();self.close();raise sqlite3.OperationalError(error)
        if cancelled:
            callback_type=C.CFUNCTYPE(C.c_int,C.c_void_p)
            self.callback=callback_type(lambda _:int(cancelled()))
            self.lib.sqlite3_progress_handler(self.pointer,10000,C.cast(self.callback,C.c_void_p),None)
    def error(self):return self.lib.sqlite3_errmsg(self.pointer).decode('utf-8',errors='replace')
    def execute(self,sql,parameters=()):return Cursor(self,sql,parameters)
    def executemany(self,sql,rows):
        for parameters in rows:
            cursor=self.execute(sql,parameters);cursor.fetchall();cursor.close()
    def close(self):
        for cursor in tuple(self.cursors):cursor.close()
        if self.pointer:
            self.lib.sqlite3_close_v2(self.pointer);self.pointer=None


def readonly(path:Path,*,cancelled=None):
    return NativeConnection(path,cancelled=cancelled)


def decoded_connection(path:Path,*,cancelled=None):
    if (runtime_directory()/'runtime.json').is_file():return NativeConnection(path,cancelled=cancelled)
    if Path(path).stat().st_size>=0x40000000-1024:
        raise RuntimeError('large NTQQ views require the verified private SQLite reader')
    result=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    if cancelled:result.set_progress_handler(lambda:int(cancelled()),10000)
    return result

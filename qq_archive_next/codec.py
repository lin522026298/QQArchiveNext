"""Lossless SQLite value encoding, including exact binary and float values."""
import base64
import json
import struct


def tagged(value):
    if value is None:return ['null']
    if isinstance(value,int):return ['int',str(value)]
    if isinstance(value,float):return ['float',struct.pack('>d',value).hex()]
    if isinstance(value,str):return ['text',value]
    if isinstance(value,(bytes,bytearray,memoryview)):return ['bytes',base64.b64encode(value).decode('ascii')]
    raise TypeError('unsupported native SQLite value')


def untag(item):
    kind=item[0]
    if kind=='null' and len(item)==1:return None
    if kind=='int':return int(item[1])
    if kind=='float':return struct.unpack('>d',bytes.fromhex(item[1]))[0]
    if kind=='text':return item[1]
    if kind=='bytes':return base64.b64decode(item[1],validate=True)
    raise ValueError('invalid SQLite value encoding')


def encode(values):
    return json.dumps([tagged(v) for v in values],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def decode(value):
    return tuple(untag(item) for item in json.loads(value))


def encode_binary(values):
    """Compact physical storage; semantic row hashes retain typed-json-v1."""
    result=bytearray(b'QQR1')
    for value in values:
        if value is None:result.append(0)
        elif isinstance(value,int):result.append(1);result.extend(struct.pack('>q',value))
        elif isinstance(value,float):result.append(2);result.extend(struct.pack('>d',value))
        else:
            is_text=isinstance(value,str)
            if not is_text and not isinstance(value,(bytes,bytearray,memoryview)):raise TypeError('unsupported native value')
            data=value.encode('utf-8') if is_text else bytes(value)
            result.append(3 if is_text else 4);result.extend(struct.pack('>Q',len(data)));result.extend(data)
    return bytes(result)


def decode_binary(data):
    if not data.startswith(b'QQR1'):raise ValueError('unsupported native row codec')
    offset=4;values=[]
    while offset<len(data):
        kind=data[offset];offset+=1
        if kind==0:values.append(None);continue
        if kind in (1,2):
            values.append(struct.unpack('>q' if kind==1 else '>d',data[offset:offset+8])[0]);offset+=8
        elif kind in (3,4):
            size=struct.unpack('>Q',data[offset:offset+8])[0];offset+=8
            value=data[offset:offset+size];offset+=size
            if len(value)!=size:raise ValueError('truncated native row value')
            values.append(value.decode('utf-8') if kind==3 else value)
        else:raise ValueError('invalid native row value tag')
    return tuple(values)

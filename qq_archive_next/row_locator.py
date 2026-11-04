"""Conservative changed-page -> native rowid windows, never time/ID watermarks.

DBSTAT supplies current ownership and overflow-cell paths. Unsupported index
trees, root changes, or any ambiguity fall back to a full table scan.
"""
from __future__ import annotations
import struct
from pathlib import Path
from .cipher import PAGE,RESERVE


def varint(data,offset):
    value=0
    for ordinal in range(8):
        byte=data[offset+ordinal];value=(value<<7)|(byte&127)
        if byte<128:return value,offset+ordinal+1
    return (value<<8)|data[offset+8],offset+9


class Tree:
    def __init__(self,path):self.stream=Path(path).open('rb');self.pages=Path(path).stat().st_size//PAGE
    def close(self):self.stream.close()
    def page(self,number):
        if not 1<=number<=self.pages:raise ValueError('B-tree page outside image')
        self.stream.seek((number-1)*PAGE);data=self.stream.read(PAGE)
        if len(data)!=PAGE:raise ValueError('truncated B-tree page')
        base=100 if number==1 else 0;flag=data[base]
        if flag not in (2,5,10,13):raise ValueError('unsupported B-tree page')
        count=int.from_bytes(data[base+3:base+5],'big');header=12 if flag in (2,5) else 8
        if base+header+count*2>PAGE-RESERVE:raise ValueError('invalid B-tree cell pointers')
        offsets=[int.from_bytes(data[base+header+2*n:base+header+2*n+2],'big') for n in range(count)]
        if any(not base+header<=offset<PAGE-RESERVE for offset in offsets):raise ValueError('invalid B-tree cell offset')
        return data,base,flag,offsets
    def descend(self,root,path):
        current=root
        for token in path.strip('/').split('/'):
            if not token:continue
            child=int(token,16);data,base,flag,offsets=self.page(current)
            if flag not in (2,5) or child>len(offsets):raise ValueError('invalid DBSTAT child path')
            offset=base+8 if child==len(offsets) else offsets[child]
            current=int.from_bytes(data[offset:offset+4],'big')
        return current
    def rows(self,root):
        stack=[root];seen=set();result=set()
        while stack:
            number=stack.pop()
            if number in seen:raise ValueError('repeated B-tree child')
            seen.add(number);data,base,flag,offsets=self.page(number)
            if flag==5:
                stack.append(int.from_bytes(data[base+8:base+12],'big'))
                stack.extend(int.from_bytes(data[offset:offset+4],'big') for offset in offsets)
            elif flag==13:
                for offset in offsets:
                    _,position=varint(data,offset);identity,_=varint(data,position)
                    result.add(identity-(1<<64) if identity>=(1<<63) else identity)
            else:raise ValueError('WITHOUT ROWID/index tree needs full scan')
        return result


def roots(connection):
    return {row[0]:[row[1],row[2],row[3]] for row in connection.execute(
      "SELECT name,tbl_name,type,rootpage FROM sqlite_master WHERE rootpage>0 ORDER BY name")}


def locate(connection,path,changed_pages,previous_roots):
    current=roots(connection)
    if previous_roots is None or changed_pages is None:
        return None,current
    connection.execute('CREATE TEMP TABLE changed_page(number INTEGER PRIMARY KEY)')
    connection.executemany('INSERT INTO changed_page VALUES(?)',((n,) for n in changed_pages))
    windows={}
    for name,value in current.items():
        if previous_roots.get(name)!=value:windows[value[0]]=None
    tree=Tree(path)
    try:
        rows=connection.execute('''SELECT d.name,d.pageno,d.pagetype,d.path,m.tbl_name,m.type,m.rootpage
          FROM dbstat d JOIN sqlite_master m ON m.name=d.name
          WHERE d.pageno IN (SELECT number FROM changed_page)''')
        for row in rows:
            name,number,pagetype,path,owner,kind,root=tuple(row)
            if owner in windows and windows[owner] is None:continue
            # Secondary indexes contain no independent native row fields. Every
            # inserted/updated row also changes a table leaf (or its overflow).
            # Index churn must not turn every new message into a full-table scan.
            if kind=='index':continue
            if kind!='table' or pagetype is None or number==root:
                windows[owner]=None;continue
            try:
                if pagetype=='overflow':
                    prefix=path.split('+',1)[0]
                    parent=prefix[:prefix.rfind('/')+1]
                    number=tree.descend(root,parent)
                window=tree.rows(number)
                windows.setdefault(owner,set()).update(window)
            except (ValueError,IndexError,struct.error):
                windows[owner]=None
    finally:tree.close()
    return {name:sorted(values) if values is not None else None for name,values in windows.items()},current

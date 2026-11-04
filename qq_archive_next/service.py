"""One cancellable writer; polling hints never advance archive receipts."""
from __future__ import annotations
import ctypes
import json
import os
import time
import uuid
from queue import Queue,Empty
from pathlib import Path
from threading import Event

from .catalog import Catalog
from .config import load,inside,key_bytes,publish
from .guard import WriterLock
from .materialize import atomic_json,update
from .materialize import file_hash


def signature(source):
    result=[]
    for path in (Path(source['database']),Path(str(source['database'])+'-wal')):
        try:
            stat=path.stat();result.append([stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns])
        except FileNotFoundError:result.append(None)
    return result


def client_version(path):
    """Read a PE version resource without executing or changing QQ."""
    if os.name!='nt':return None
    import ctypes.wintypes as W
    lib=ctypes.windll.version
    lib.GetFileVersionInfoSizeW.argtypes=[W.LPCWSTR,ctypes.POINTER(W.DWORD)]
    lib.GetFileVersionInfoW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID]
    lib.VerQueryValueW.argtypes=[W.LPCVOID,W.LPCWSTR,ctypes.POINTER(W.LPVOID),ctypes.POINTER(W.UINT)]
    length=lib.GetFileVersionInfoSizeW(str(path),None)
    if not length:raise ValueError('cannot read configured QQ client version')
    buffer=ctypes.create_string_buffer(length)
    if not lib.GetFileVersionInfoW(str(path),0,length,buffer):raise ValueError('QQ version resource unavailable')
    pointer=W.LPVOID();size=W.UINT()
    if not lib.VerQueryValueW(buffer,'\\',ctypes.byref(pointer),ctypes.byref(size)):raise ValueError('QQ version unavailable')
    words=ctypes.cast(pointer,ctypes.POINTER(W.DWORD))
    return '.'.join(map(str,(words[2]>>16,words[2]&65535,words[3]>>16,words[3]&65535)))


class Coordinator:
    def __init__(self,root,*,stop=None,notify=None):
        self.root,self.config=load(root)
        self.stop=stop or Event();self.notify=notify or (lambda status:None)
        self.nonce=uuid.uuid4().hex
        status_path=self.root/'status.private.json'
        try:previous=json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):previous={}
        self.status={'schema':'qq-next-status/1','state_id':self.config['state_id'],
          'runtime':self.nonce,'pid':os.getpid(),'state':'starting','protocol':3,
          'sources':previous.get('sources',{}) if previous.get('state_id')==self.config['state_id'] else {}}
        import psutil
        self.status['process_started']=psutil.Process().create_time()
        self.last_signature={};self.last_attempt={};self.last_full={};self.last_media={};self.last_deep={}
        for identity,value in self.status['sources'].items():
            checked=value.get('last_structural_check')
            if isinstance(checked,(int,float)) and 0<checked<=time.time():self.last_deep[identity]=checked
        self.last_control=0.0
        self.handled_control=None
        self.commands=Queue()
        self.last_backup_check=0.0
        self.pending_requests=set()
        self.active_request=None
        self.retry_after={};self.media_retry_after={}
        wall=time.time();mono=time.monotonic()
        for identity,value in self.status['sources'].items():
            for field,target in [('capture_issue',self.retry_after),('media_issue',self.media_retry_after)]:
                due=value.get(field,{}).get('next_retry',0)
                if isinstance(due,(int,float)) and wall<due<=wall+900:target[identity]=mono+due-wall

    def save_status(self):
        self.status['updated']=time.time()
        atomic_json(self.root/'status.private.json',self.status)
        self.notify(dict(self.status))

    def check_version(self):
        expected=self.config.get('client_version');path=self.config.get('client_exe')
        if expected and path:
            actual=client_version(path);self.status['client_version']=actual
            if actual!=expected:raise ValueError('QQ client version changed; capture stopped pending a tested new binding')

    def cancelled(self):
        if self.stop.is_set():return True
        now=time.monotonic()
        if now-self.last_control<0.5:return False
        self.last_control=now
        try:
            request=json.loads((self.root/'control.private.json').read_text(encoding='utf-8'))
            if request.get('state_id')==self.config['state_id'] and request.get('runtime')==self.nonce:
                command=request.get('command')
                if command=='stop':self.stop.set()
                elif command in ('backup','verify') and request.get('request_id') and request['request_id']!=self.handled_control:
                    self.handled_control=request['request_id']
                    self.commands.put((command,{'repository':self.root.parent/'backup-repository'}))
        except (OSError,ValueError):pass
        pending=self.root/'requests/pending'
        for path in sorted(pending.glob('*.json')):
            if len(path.stem)!=32 or any(c not in '0123456789abcdef' for c in path.stem):continue
            if path.stem in self.pending_requests:continue
            try:
                request=json.loads(path.read_text(encoding='utf-8'))
                if request.get('state_id')!=self.config['state_id']:continue
                if request.get('runtime')!=self.nonce:
                    atomic_json(self.root/'requests/results'/path.name,{'action':request.get('command'),'state':'interrupted','request_id':path.stem,
                      'finished':time.time(),'error':'上一次归档实例已结束，操作结果未确认；请先核对已有备份点或任务结果再重试。'})
                    path.unlink(missing_ok=True);(self.root/'requests/cancel'/path.stem).unlink(missing_ok=True)
                    continue
                if request.get('command') not in ('backup','verify','capture','media','settings','pin','retry'):continue
                options=request.get('options',{})
                if not isinstance(options,dict):continue
                self.pending_requests.add(path.stem)
                self.commands.put((request['command'],options|{'request_id':path.stem}))
            except (OSError,ValueError):continue
        return self.stop.is_set() or bool(self.active_request and (self.root/'requests/cancel'/self.active_request).exists())

    def capture(self,catalog,source,*,force=False,deep=True):
        identity=source['id'];view=self.root/'views'/identity/'current.sqlite'
        self.status['state']='capturing';self.status['active_source']=identity;self.save_status()
        proof=update(Path(source['database']),view,key_bytes(self.root,source),cancelled=self.cancelled,deep=deep)
        known=catalog.db.execute('SELECT binding FROM source WHERE id=?',(identity,)).fetchone()
        if known and known['binding']!=proof['binding']:raise ValueError('source generation/catalog binding mismatch')
        result={'rows_scanned':0,'versions_added':0,'no_op':True}
        if force or proof['needs_scan'] or known is None:
            # Missing pending metadata, previous crash, or explicit full means
            # full reconciliation. Only a freshly authenticated window may narrow it.
            windows=proof['row_windows'] if not force and known else None
            last_progress=[0.0]
            def progress(rows,added):
                if time.monotonic()-last_progress[0]>=2:
                    self.status['progress']={'rows_scanned':rows,'versions_added':added,'committed':False}
                    self.save_status();last_progress[0]=time.monotonic()
            result=catalog.import_sqlite(view,source_id=identity,account_key=source['account_key'],binding=proof['binding'],
              cut={key:value for key,value in proof.items() if key not in ('row_windows','touched_tables')},
              row_windows=windows,cancelled=self.cancelled,progress=progress)
            # Only the catalog transaction's success clears the recovery obligation.
            view.with_suffix('.pending').unlink(missing_ok=True)
        self.status.pop('progress',None)
        now=time.time()
        if proof['validation']=='sqlite_quick_check':self.last_deep[identity]=now
        previous=self.status['sources'].get(identity,{})
        previous_media=previous.get('media')
        self.status['sources'][identity]={'last_success':now,'state':'ok','capture':proof|{'row_windows':None},'import':result}
        if previous_media is not None:self.status['sources'][identity]['media']=previous_media
        if previous.get('media_issue'):self.status['sources'][identity]['media_issue']=previous['media_issue']
        if previous.get('capture_issue'):
            from .health import event
            event(self.root,'recovered',source_id=identity,component='capture',code=previous['capture_issue']['code'])
        self.retry_after.pop(identity,None)
        self.status['sources'][identity]['last_structural_check']=self.last_deep.get(identity)
        self.last_signature[identity]=signature(source)
        self.last_full[identity]=time.monotonic()
        return result

    def failed(self,source,error,component):
        from .health import retry_failure,event
        identity=source['id'];old=self.status['sources'].setdefault(identity,{})
        field='capture_issue' if component=='capture' else 'media_issue'
        failure=retry_failure(error,old.get(field),interval=self.config['capture_seconds'])
        old[field]=failure
        if component=='capture':old.update(state='retry',error=str(error),last_attempt=time.time())
        target=self.retry_after if component=='capture' else self.media_retry_after
        target[identity]=time.monotonic()+failure['retry_seconds']
        event(self.root,'retry',source_id=identity,component=component,**failure)

    def capture_media(self,source,*,full=False):
        from .media import capture_media
        from .health import event
        identity=source['id'];self.status['state']='media';self.status['active_source']=identity;self.save_status()
        report=capture_media(self.root,source,full=full,cancelled=self.cancelled)
        old=self.status['sources'].setdefault(identity,{})
        if old.get('media_issue'):event(self.root,'recovered',source_id=identity,component='media',code=old['media_issue']['code'])
        old.pop('media_issue',None);old['media']=report;old['last_media_check']=time.time()
        self.last_media[identity]=time.monotonic();self.media_retry_after.pop(identity,None)
        return report

    def maintenance(self,catalog):
        try:command,options=self.commands.get_nowait()
        except Empty:return False
        self.active_request=options.get('request_id')
        self.status['state']=command
        self.status['maintenance']={'action':command,'state':'running','request_id':self.active_request,'started':time.time()}
        self.save_status()
        try:
            last_progress=[0.0]
            def progress(phase,rows):
                if time.monotonic()-last_progress[0]<2 and self.status.get('progress',{}).get('phase')==phase:return
                self.status['progress']={'phase':phase,'checked':rows}
                self.save_status();last_progress[0]=time.monotonic()
            if command=='backup':
                from .backup import create
                result=create(self.root,options['repository'],include_keys=options.get('include_keys',False),pin=options.get('pin',False),cancelled=self.cancelled)
                atomic_json(self.root/'backup_receipt.private.json',{'revision':self.archive_revision(catalog),'point':result['id'],'finished':time.time()})
            elif command=='verify':
                result=catalog.verify(cancelled=self.cancelled,progress=progress)
                if self.config['schema']=='qq-next-config/2':
                    from .history import verify
                    result['history']=verify(self.root,self.config,cancelled=self.cancelled,progress=progress)
                if (self.root/'assets.sqlite').exists():
                    from .media import verify
                    result['media']=verify(self.root,cancelled=self.cancelled)
            elif command in ('capture','media'):
                result={}
                for source in self.config['sources']:
                    item={}
                    if command=='capture':
                        self.check_version()
                        item['capture']=self.capture(catalog,source,force=bool(options.get('full')),deep=True)
                    if source.get('media_root') and (command=='media' or options.get('media',True)):
                        item['media']=self.capture_media(source,full=bool(options.get('full')))
                    result[source['id']]=item
            elif command=='settings':
                from .product import write_settings
                result=write_settings(self.root,options.get('settings',{}))
                self.root,self.config=load(self.root)
            elif command=='pin':
                from .product import Product
                result=Product(self.root)._pin(Path(options['repository']),options)
            elif command=='retry':
                identity=options.get('source_id')
                if identity not in {s['id'] for s in self.config['sources']}:raise ValueError('unknown source')
                self.retry_after.pop(identity,None);self.media_retry_after.pop(identity,None)
                self.last_signature.pop(identity,None);self.last_full.pop(identity,None);self.last_attempt.pop(identity,None)
                self.last_media.pop(identity,None)
                result={'source_id':identity,'state':'retry_scheduled'}
            else:raise ValueError('unknown maintenance action')
            self.status['maintenance']={'action':command,'state':'ok','finished':time.time(),'result':result,'request_id':self.active_request}
        except InterruptedError:
            self.status['maintenance']={'action':command,'state':'cancelled','finished':time.time(),'request_id':self.active_request}
            if self.stop.is_set():raise
        except Exception as exc:
            self.status['maintenance']={'action':command,'state':'error','finished':time.time(),'error':str(exc),'request_id':self.active_request}
            from .health import event,diagnose
            event(self.root,'maintenance_failed',operation=command,**diagnose(exc))
        finally:
            self.status.pop('progress',None)
            from .health import outcome
            result_state=self.status['maintenance']
            outcome(self.root,command,'completed' if result_state['state']=='ok' else 'failed' if result_state['state']=='error' else 'cancelled',
              finished=result_state.get('finished'),error=result_state.get('error'))
            if self.active_request:
                atomic_json(self.root/'requests/results'/(self.active_request+'.json'),self.status['maintenance'])
                (self.root/'requests/pending'/(self.active_request+'.json')).unlink(missing_ok=True)
                (self.root/'requests/cancel'/self.active_request).unlink(missing_ok=True)
                self.pending_requests.discard(self.active_request)
            self.active_request=None
        self.save_status();return True

    def archive_revision(self,catalog):
        revision=[catalog.db.execute('SELECT coalesce(max(id),0) FROM capture').fetchone()[0],0,file_hash(self.root/'config.private.json')]
        if (self.root/'assets.sqlite').exists():
            from .media import connect
            assets=connect(self.root,readonly=True)
            try:revision[1]=assets.execute('SELECT coalesce(max(id),0) FROM observation').fetchone()[0]
            finally:assets.close()
        manifest=inside(self.root,self.config['application_directory']+'/release_verification.json') if self.config.get('application_directory') else None
        revision.append(file_hash(manifest) if manifest and manifest.is_file() else None)
        return revision

    def automatic_backup(self,catalog):
        now=time.monotonic()
        if not self.config.get('auto_backup') or now-self.last_backup_check<self.config['backup_seconds']:return
        self.last_backup_check=now
        revision=self.archive_revision(catalog)
        try:previous=json.loads((self.root/'backup_receipt.private.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):previous={}
        if previous.get('revision')==revision:return
        self.commands.put(('backup',{'repository':self.root.parent/'backup-repository'}))

    def run(self,*,once=False,force=False,media=True,capture_sources=True):
        with WriterLock(self.root):
            try:return self._run(once=once,force=force,media=media,capture_sources=capture_sources)
            except InterruptedError:raise
            except Exception as exc:
                from .health import diagnose,event
                failure=diagnose(exc)
                self.status.update(state='stopped',fatal=failure);self.save_status()
                event(self.root,'worker_failed',**failure)
                raise

    def _run(self,*,once=False,force=False,media=True,capture_sources=True):
        if capture_sources and self.config.get('integration_origin_state'):
            import sqlite3
            check=sqlite3.connect((self.root/'catalog.sqlite').as_uri()+'?mode=ro',uri=True)
            try:
                phase=check.execute('SELECT closed FROM unified_origin WHERE state_id=?',(self.config['integration_origin_state'],)).fetchone()
                if not phase or not phase[0]:raise ValueError('归档整合尚未切换完成；可阅读或备份，暂不能开始采集')
            finally:check.close()
        if capture_sources:self.check_version()
        catalog=Catalog(self.root/'catalog.sqlite')
        publish(self.root,self.config)
        self.save_status()
        try:
            while not self.cancelled():
                now=time.monotonic();changed=False
                try:
                    if self.maintenance(catalog):changed=True
                except InterruptedError:self.stop.set();break
                for source in self.config['sources'] if capture_sources else ():
                    if self.cancelled():break
                    identity=source['id']
                    due=once or force or identity not in self.last_signature or now-self.last_full.get(identity,0)>=self.config['full_verify_seconds']
                    due=due or (signature(source)!=self.last_signature.get(identity) and now-self.last_attempt.get(identity,0)>=self.config['capture_seconds'])
                    media_due=media and source.get('media_root') and (once or now-self.last_media.get(identity,0)>=self.config['media_seconds'])
                    if not once and not force:
                        due=due and now>=self.retry_after.get(identity,0)
                        media_due=media_due and now>=self.media_retry_after.get(identity,0)
                    if not due and not media_due:continue
                    changed=True
                    try:
                        if due:
                            self.last_attempt[identity]=time.monotonic()
                            self.check_version()
                            deep=force or time.time()-self.last_deep.get(identity,0)>=self.config['full_verify_seconds']
                            self.capture(catalog,source,force=force,deep=deep)
                    except InterruptedError:
                        self.stop.set();break
                    except Exception as exc:
                        self.failed(source,exc,'capture')
                    if media_due and not self.cancelled():
                        try:self.capture_media(source)
                        except InterruptedError:self.stop.set();break
                        except Exception as exc:self.failed(source,exc,'media')
                self.status['state']='stopping' if self.cancelled() else 'idle'
                self.status.pop('active_source',None)
                if changed or once:self.save_status()
                if once:
                    while not self.cancelled() and self.maintenance(catalog):pass
                    break
                self.automatic_backup(catalog)
                force=False
                # Event.wait permits immediate stop without a blocking GUI call.
                self.stop.wait(self.config['poll_seconds'])
        finally:
            catalog.close();self.status['state']='stopped';self.save_status()
        return self.status


def request_action(root,command):
    if command not in ('stop','backup','verify'):raise ValueError('unsupported runtime action')
    root,config=load(root)
    status=json.loads((root/'status.private.json').read_text(encoding='utf-8'))
    if status.get('state_id')!=config['state_id']:raise ValueError('foreign runtime status')
    atomic_json(root/'control.private.json',{'state_id':config['state_id'],'runtime':status['runtime'],'command':command,'request_id':uuid.uuid4().hex})


def request_stop(root):request_action(root,'stop')


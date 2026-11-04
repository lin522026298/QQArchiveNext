"""Shared application operations for WebView, browser and headless agents.

Reads open their own read-only connections. The capture process remains the
only writer; maintenance requests cross a durable, runtime-bound local inbox.
"""
from __future__ import annotations
import base64
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

from .config import load, inside
from .materialize import atomic_json

OPERATIONS = {
    'status': '归档状态、源健康及后台优先级',
    'accounts': '所有统一账号与记录概况',
    'conversations': '账号会话列表；query、kind 可筛选',
    'messages': '会话分页；before/after、around 或 at 时间定位',
    'timeline': '账号全部会话的时间线；before/after 或 at 定位',
    'search': '全部新旧记录的字面搜索；支持分页和取消',
    'observations': '原生行、来源、版本、采集切面及历史媒体关联',
    'media': '指定消息的媒体；可请求 voice WAV 转换',
    'archive.start': '开启后台持续归档',
    'archive.stop': '安全暂停持续归档',
    'capture': '立即采集；full 可完整对账',
    'media.scan': '立即媒体巡检；full 可重新哈希源文件',
    'verify': '校验当前库、全部历史及媒体',
    'backup': '完整去重备份；支持固定备份点',
    'backups': '备份点列表与完整性边界',
    'backup.pin': '固定或取消固定已有备份点',
    'backup.verify': '重建并哈希备份点内的每个文件',
    'restore': '恢复至新目录；默认全语义验证',
    'export-portable': '导出含程序和资料的完整迁移包',
    'settings': '读取或修改检查间隔与自动归档、备份设置',
    'jobs': '读取共享界面的任务列表',
    'job': '读取指定共享任务和结果；参数 id',
    'job.cancel': '安全取消共享任务；参数 id',
    'web': '启闭共享浏览器入口；参数 enabled',
    'app.quit': '安全退出共享应用与后台归档',
    'health': '原因、处理入口、自动重试时间与近期诊断事件',
    'source.retry': '立即安排指定来源重新检查；source_id',
    'desktop.settings': '登录自启、托盘关闭行为和重要通知设置',
    'desktop.shortcuts': '创建本工具的桌面与后台运行快捷方式',
}


def json_value(value):
    """Do not round 64-bit evidence identifiers in JavaScript."""
    if dataclasses.is_dataclass(value):value=dataclasses.asdict(value)
    if isinstance(value,dict):return {str(k):json_value(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [json_value(v) for v in value]
    if isinstance(value,Path):return str(value)
    if isinstance(value,bytes):return {'type':'bytes','hex':value.hex()}
    if isinstance(value,int) and abs(value)>2**53-1:return str(value)
    return value


def capabilities():
    return {'schema':'qq-next-agent/1','operations':OPERATIONS,'arguments':{
        'reader':{'account':'account_key','conversation':'opaque conversation key',
          'msg_id':'decimal string','before/after':{'timestamp':'integer or null','msg_id':'decimal string'},
          'at':'Unix seconds; timeline/messages date positioning','limit':'1..200','offset':'0..100000'},
        'search':{'query':'literal text, at least 2 characters'},
        'backup/restore/export-portable':{'repository':'absolute path; defaults to sibling backup-repository',
          'point':'64 hex digest or omitted for latest','destination':'new absolute path for restore/export',
          'reuse_immutable':'restore only; optional same-volume immutable hardlinks; default false',
          'file_check_only':'restore only; skips semantic verification; default false'},
        'settings':{'settings':'poll_seconds, capture_seconds, full_verify_seconds, media_seconds, backup_seconds, auto_capture, auto_backup'},
      },'safety':{'network':'loopback only','restore':'new directory only','backup':'includes all frozen originals and source keys for unified archives',
        'ordinary_folder_copy':'portable, no absolute dependency on the original PC','credentials':'never returned by the API'}}


def job_paths(root):
    paths=[]
    for path in inside(root,'ui/jobs').glob('*.json'):
        if len(path.stem)!=32 or any(c not in '0123456789abcdef' for c in path.stem):continue
        try:paths.append((path.stat().st_mtime,path))
        except FileNotFoundError:continue
    return [path for _,path in sorted(paths,reverse=True)]


def job_summaries(root):
    values=[]
    for path in job_paths(root)[:40]:
        try:value=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        value.pop('result',None);values.append(value)
    return values


def runtime(root):
    import psutil
    root,config=load(root)
    try:
        value=json.loads((root/'status.private.json').read_text(encoding='utf-8'))
        process=psutil.Process(value['pid'])
        if value.get('state_id')!=config['state_id'] or abs(process.create_time()-value['process_started'])>.05 or value.get('state')=='stopped':return None
        value['priority']={'cpu':process.nice(),'io':process.ionice() if os.name=='nt' else None}
        return value
    except (OSError,ValueError,KeyError,psutil.Error):return None


def cli_prefix():
    return [sys.executable] if getattr(sys,'frozen',False) else [sys.executable,'-m','qq_archive_next']


def spawn_watch(root):
    flags=0
    if os.name=='nt':flags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_BREAKAWAY_FROM_JOB
    command=cli_prefix()+['--state',str(root),'watch']
    kwargs={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True}
    if os.name=='nt':kwargs['creationflags']=flags
    else:kwargs['start_new_session']=True
    try:return subprocess.Popen(command,**kwargs)
    except PermissionError:
        if os.name!='nt':raise
        kwargs['creationflags']=flags & ~subprocess.CREATE_BREAKAWAY_FROM_JOB
        return subprocess.Popen(command,**kwargs)


def write_settings(root,changes):
    root,config=load(root)
    allowed={'poll_seconds','capture_seconds','full_verify_seconds','media_seconds','backup_seconds','auto_capture','auto_backup'}
    if not isinstance(changes,dict) or set(changes)-allowed:raise ValueError('unsupported settings')
    for name,value in changes.items():
        if name.startswith('auto_'):
            if not isinstance(value,bool):raise ValueError('setting requires boolean: '+name)
        elif isinstance(value,bool) or not isinstance(value,(int,float)):raise ValueError('setting requires seconds: '+name)
    proposed=config|changes
    limits={'poll_seconds':(1,300),'capture_seconds':(5,86400),'full_verify_seconds':(30,604800),'media_seconds':(10,86400),'backup_seconds':(60,604800)}
    for name,(minimum,maximum) in limits.items():
        if not minimum<=proposed[name]<=maximum:raise ValueError('invalid interval: '+name)
    atomic_json(root/'config.private.json',proposed)
    return {name:proposed.get(name,False) for name in sorted(allowed)}


def request_writer(root,command,options,cancelled):
    status=runtime(root)
    if not status:raise ValueError('background writer is not running')
    if status.get('protocol',1)<2:raise ValueError('后台仍是旧版；请安全重启归档后使用此操作')
    if command=='retry' and status.get('protocol',1)<3:raise ValueError('后台尚未升级：请安全暂停后重新开启归档，再重新检查此来源。')
    identity=uuid.uuid4().hex
    atomic_json(root/'requests/pending'/(identity+'.json'),{'state_id':status['state_id'],'runtime':status['runtime'],'command':command,'options':options})
    path=root/'requests/results'/(identity+'.json')
    while not path.exists():
        if cancelled():
            flag=root/'requests/cancel'/identity;flag.parent.mkdir(parents=True,exist_ok=True);flag.touch()
        current=runtime(root)
        if not current or current['runtime']!=status['runtime']:raise InterruptedError('background writer stopped; the request was not confirmed')
        time.sleep(.2)
    result=json.loads(path.read_text(encoding='utf-8'))
    if result['state']=='cancelled':raise InterruptedError('operation cancelled')
    if result['state']!='ok':raise ValueError(result.get('error','writer operation failed'))
    return result['result']


def conversation_key(conversation):
    data={k:getattr(conversation,k) for k in ('source_table','id_column','conversation_id','qq_value','uid_values')}
    return base64.urlsafe_b64encode(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()).decode().rstrip('=')


def conversation_value(conversation):
    return json_value(dataclasses.asdict(conversation)|{'key':conversation_key(conversation)})


class Product:
    def __init__(self,root):
        self.root,self.config=load(root)
        self.missing_since=None

    def _store(self,account,cancelled):
        from .reader import NextArchiveStore
        _,config=load(self.root)
        if account not in {s['account_key'] for s in config['sources']}:raise ValueError('unknown account')
        return NextArchiveStore(inside(self.root,'viewer-'+account+'.json'),cancelled=cancelled)

    def _conversation(self,store,key):
        # The client supplies an opaque identity, never an SQL fragment or table.
        for item in store.conversations():
            if conversation_key(item)==key:return item
        raise ValueError('conversation no longer available')

    def _destination(self,value,repo,*,portable_restore=False):
        if not value or not Path(value).is_absolute():raise ValueError('destination must be a new absolute path')
        path=Path(value).resolve()
        _,config=load(self.root)
        protected=[self.root]+[Path(s['database']).resolve().parent for s in config['sources']]
        if not (portable_restore and path==repo/'Recovered' and (repo/'PORTABLE_RECEIPT.json').is_file()):protected.append(repo)
        protected += [Path(s['media_root']).resolve() for s in config['sources'] if s.get('media_root')]
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):raise ValueError('destination overlaps source, state or backup repository')
        return path

    def execute(self,operation,args=None,*,cancelled=lambda:False):
        if operation not in OPERATIONS:raise ValueError('unknown operation')
        args=args or {}
        if not isinstance(args,dict):raise ValueError('arguments must be an object')
        if cancelled():raise InterruptedError('cancelled before starting')
        root=self.root
        if operation in ('jobs','job','job.cancel','web','app.quit'):
            from .agent import host_call
            return host_call(root,operation,args)
        _,config=load(root)
        if operation=='desktop.settings':
            from .desktop import settings
            return settings(root,args.get('settings'))
        if operation=='desktop.shortcuts':
            from .desktop import shortcuts
            return shortcuts(root,directory=args.get('directory'))
        if operation=='health':
            return self.execute('status')['health']
        if operation=='source.retry':
            identity=args.get('source_id')
            if identity not in {s['id'] for s in config['sources']}:raise ValueError('unknown source')
            if not runtime(root):return self.execute('archive.start',cancelled=cancelled)
            return request_writer(root,'retry',{'source_id':identity},cancelled)
        repo=Path(args.get('repository') or root.parent/'backup-repository').resolve()
        if operation=='status':
            active=runtime(root)
            last=json.loads((root/'status.private.json').read_text(encoding='utf-8')) if (root/'status.private.json').exists() else {}
            if active or not config.get('auto_capture'):self.missing_since=None
            elif self.missing_since is None:self.missing_since=time.time()
            from .health import report
            from .desktop import settings as desktop_settings
            from .desktop import application as desktop_application
            try:agent_executable=str(desktop_application(root)/'backend/QQArchiveCore/QQArchiveCore.exe')
            except ValueError:agent_executable=None
            jobs=job_summaries(root)
            return {'running':bool(active),'capture':active or last,'settings':self.execute('settings'),
              'desktop':desktop_settings(root),'jobs':jobs,'health':report(root,config,active,active or last,jobs,missing_since=self.missing_since),
              'state_directory':str(root),'repository':str(repo),'application':config.get('application_directory'),
              'agent_executable':agent_executable,
              'source_details':[{'id':s['id'],'account_key':s['account_key'],'database':s['database'],'media_root':s.get('media_root'),
                'expected_client_version':config.get('client_version'),'observed_client_version':(active or last).get('client_version')} for s in config['sources']],
              'legacy_sources':[{k:s.get(k) for k in ('id','account_key','manifest','message_store')} for s in config.get('legacy_sources',[])],
              'history_packages':config.get('history_packages',[]),'recovery_packages':config.get('recovery_packages',[])}
        if operation=='settings':
            changes=args.get('settings')
            if changes is None:return {k:config.get(k,False) for k in ('poll_seconds','capture_seconds','full_verify_seconds','media_seconds','backup_seconds','auto_capture','auto_backup')}
            if runtime(root):return request_writer(root,'settings',{'settings':changes},cancelled)
            from .guard import WriterLock
            with WriterLock(root):return write_settings(root,changes)
        if operation=='accounts':
            values=[]
            for key in dict.fromkeys(s['account_key'] for s in config['sources']):
                store=self._store(key,cancelled)
                try:values.append({'account_key':key,**store.overview()})
                finally:store.close()
            return values
        if operation in ('conversations','messages','timeline','search','observations','media'):
            store=self._store(args.get('account'),cancelled)
            try:
                with store._snapshot():return self._read(store,operation,args,cancelled)
            finally:store.close()
        if operation=='archive.start':
            if runtime(root):return {'running':True,'already_running':True}
            from .guard import WriterLock
            with WriterLock(root):write_settings(root,{'auto_capture':True})
            process=spawn_watch(root)
            for _ in range(100):
                if runtime(root):return {'running':True,'pid':runtime(root)['pid']}
                if process.poll() is not None:raise ValueError('background capture did not start; inspect source binding and writer lock')
                time.sleep(.1)
            raise ValueError('background capture startup timed out')
        if operation=='archive.stop':
            active=runtime(root)
            if active:
                from .service import request_stop
                request_stop(root)
                while runtime(root):
                    if cancelled():raise InterruptedError('stop is still pending')
                    time.sleep(.2)
            from .guard import WriterLock
            with WriterLock(root):write_settings(root,{'auto_capture':False})
            return {'running':False,'state':'stopped'}
        if operation in ('capture','media.scan','verify','backup'):
            command='media' if operation=='media.scan' else operation
            options={'repository':str(repo),'include_keys':bool(args.get('include_keys')),'pin':bool(args.get('pin')),
              'full':bool(args.get('full')),'media':not bool(args.get('no_media'))}
            if runtime(root):return request_writer(root,command,options,cancelled)
            from .service import Coordinator
            stop=threading.Event()
            coordinator=Coordinator(root,stop=stop)
            # A one-shot maintenance worker uses exactly the same writer path.
            original=coordinator.cancelled
            coordinator.cancelled=lambda: cancelled() or original()
            coordinator.commands.put((command,options))
            result=coordinator.run(once=True,media=False,capture_sources=False)
            receipt=result.get('maintenance',{})
            if receipt.get('state')=='cancelled':raise InterruptedError('operation cancelled')
            if receipt.get('state')!='ok':
                if cancelled():raise InterruptedError('operation cancelled')
                raise ValueError(receipt.get('error','operation was not completed'))
            return receipt['result']
        if operation=='backups':
            from .backup import repository,read_point
            if not repo.exists():return {'repository':str(repo),'points':[]}
            repo=repository(repo)
            refs=json.loads((repo/'refs.json').read_text(encoding='utf-8')) if (repo/'refs.json').exists() else {'recent':[],'pinned':[]}
            values=[]
            for path in sorted((repo/'points').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
                if cancelled():raise InterruptedError('listing cancelled')
                _,point=read_point(repo,path.stem);body=point['body'];files=body['files']
                values.append({'id':point['id'],'created':point['created'],'schema':body['schema'],
                  'files':files.get('count',len(files)),'logical_bytes':files.get('logical_bytes'),
                  'pinned':point['id'] in refs['pinned'],'complete_originals':bool(body.get('history_included')),
                  'includes_source_keys':body.get('includes_source_keys',False)})
            return {'repository':str(repo),'points':values}
        if operation=='backup.pin':
            from .backup import read_point
            from .guard import WriterLock
            # Serialize refs updates against the live writer's backup operation.
            if runtime(root):return request_writer(root,'pin',{'repository':str(repo),'point':args.get('point'),'pinned':args.get('pinned',True)},cancelled)
            with WriterLock(root):return self._pin(repo,args)
        if operation=='backup.verify':
            from .backup import verify_point
            return verify_point(repo,args.get('point'),cancelled=cancelled)
        if operation=='restore':
            from .backup import restore
            destination=self._destination(args.get('destination'),repo,portable_restore=True)
            return restore(repo,destination,identity=args.get('point'),deep=not args.get('file_check_only',False),
              cancelled=cancelled,hardlink_immutable=bool(args.get('reuse_immutable')))
        if operation=='export-portable':
            from .backup_unified import export
            from .desktop import application
            try:tool=application(root)
            except ValueError:tool=None
            return export(repo,self._destination(args.get('destination'),repo),identity=args.get('point'),cancelled=cancelled,application=tool)
        raise ValueError('unsupported operation')

    def _pin(self,repo,args):
        from .backup import read_point
        repo,point=read_point(repo,args.get('point'))
        refs=json.loads((repo/'refs.json').read_text(encoding='utf-8'))
        refs['pinned']=[p for p in refs['pinned'] if p!=point['id']]
        if args.get('pinned',True):refs['pinned'].append(point['id'])
        atomic_json(repo/'refs.json',refs)
        return {'point':point['id'],'pinned':point['id'] in refs['pinned']}

    def _read(self,store,operation,args,cancelled):
        if operation=='conversations':return [conversation_value(c) for c in store.conversations(str(args.get('query','')),str(args.get('kind','全部')))]
        if operation=='observations':return json_value(store.observations(int(args['msg_id'])))
        if operation=='search':
            limit=int(args.get('limit',100));offset=int(args.get('offset',0))
            if not 1<=limit<=200 or not 0<=offset<=100000:raise ValueError('invalid search page')
            hits=store.search(str(args.get('query','')),limit=limit,offset=offset,cancelled=cancelled)
            return [json_value(dataclasses.asdict(h)|{'msg_id':str(h.msg_id),'conversation':conversation_value(h.conversation)}) for h in hits]
        if operation=='media':
            table=args.get('source_table')
            if table not in ('c2c_messages','group_messages'):raise ValueError('invalid message table')
            rows=list(store.message_connection.execute(f'SELECT {store._message_columns(table)} FROM {table} WHERE msg_id=?',(int(args['msg_id']),)))
            messages=store._rows_to_messages(table,rows)
            links=store.media_for_messages(table,messages)
            values=[]
            for link in links.get(int(args['msg_id']),[]):
                value=json_value(link)
                if args.get('voice') and link.media_type=='voice' and link.primary and link.primary.path:
                    from .media import voice_wav
                    wav=voice_wav(self.root,link.primary.path,cancelled=cancelled)
                    value['voice_path']=str(wav)
                values.append(value)
            return values
        limit=int(args.get('limit',200))
        if not 1<=limit<=200:raise ValueError('invalid page size')
        if operation=='messages':
            conversation=self._conversation(store,args.get('conversation'))
            if args.get('around') is not None:messages=store.messages_around(conversation,int(args['around']),limit=limit)
            else:
                before=args.get('before');after=args.get('after')
                if args.get('at') is not None:after={'timestamp':int(args['at']),'msg_id':'0'}
                cursor=lambda v: (v.get('timestamp'),int(v['msg_id'])) if v else None
                messages=store._message_query(conversation,before=cursor(before),after=cursor(after),limit=limit)
        else:
            condition='account_key=?';parameters=[store.catalog_account_key];order='DESC'
            before=args.get('before');after=args.get('after')
            if before and after:raise ValueError('conflicting cursors')
            if args.get('at') is not None:after={'timestamp':int(args['at']),'msg_id':'0'}
            cursor=after or before
            if cursor:
                stamp=cursor.get('timestamp');mid=int(cursor['msg_id'])
                if after:
                    order='ASC'
                    if stamp is None:condition+=' AND (timestamp IS NOT NULL OR (timestamp IS NULL AND id>?))';parameters.append(mid)
                    else:condition+=' AND (timestamp>? OR (timestamp=? AND id>?))';parameters.extend((stamp,stamp,mid))
                else:
                    if stamp is None:condition+=' AND timestamp IS NULL AND id<?';parameters.append(mid)
                    else:condition+=' AND (timestamp<? OR (timestamp=? AND id<?) OR timestamp IS NULL)';parameters.extend((stamp,stamp,mid))
            found=list(store.message_connection.execute(f'SELECT id,kind FROM current_message WHERE {condition} ORDER BY timestamp {order},id {order} LIMIT ?',(*parameters,limit)))
            messages=[]
            for table,kind in [('c2c_messages','c2c'),('group_messages','group')]:
                ids=[row[0] for row in found if row[1]==kind]
                if ids:
                    rows=list(store.message_connection.execute(f'SELECT {store._message_columns(table)} FROM {table} WHERE msg_id IN ('+','.join('?' for _ in ids)+')',ids))
                    messages.extend(store._rows_to_messages(table,rows))
            messages.sort(key=lambda m:(m.timestamp is not None,m.timestamp or 0,m.msg_id))
        by_table={}
        for item in messages:by_table.setdefault(item.source_table,[]).append(item)
        links={}
        for table,items in by_table.items():links.update(store.media_for_messages(table,items))
        return [json_value(dataclasses.asdict(item)|{'msg_id':str(item.msg_id),'media':links.get(item.msg_id,[])}) for item in messages]


class Jobs:
    """One bounded application queue; UI state stays responsive during long reads."""
    def __init__(self,product):
        self.product=product;self.root=inside(product.root,'ui/jobs');self.queue=Queue(maxsize=32)
        self.events={};self.lock=threading.Lock();self.closing=threading.Event()
        self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'results').mkdir(exist_ok=True)
        for path in job_paths(product.root):
            value=json.loads(path.read_text(encoding='utf-8'))
            if 'result' in value:
                atomic_json(self.root/'results'/path.name,value.pop('result'));value['has_result']=True
                atomic_json(path,value)
            if value.get('state') in ('queued','running'):
                value.update(state='interrupted',finished=time.time(),error='上次应用结束，操作未确认完成；原始资料保留')
                atomic_json(path,value)
            from .health import outcome
            outcome(product.root,value.get('operation'),value.get('state'),finished=value.get('finished',value.get('created',0)),error=value.get('error'),job_id=value.get('id'),created=value.get('created'))
        self.worker=threading.Thread(target=self._run,daemon=True);self.worker.start()

    def submit(self,operation,args):
        if operation not in OPERATIONS:raise ValueError('unknown operation')
        if self.closing.is_set():raise ValueError('application is closing')
        identity=uuid.uuid4().hex
        with self.lock:
            if self.queue.full():raise ValueError('task queue is full')
            self.events[identity]=threading.Event()
            metadata={'id':identity,'operation':operation,'state':'queued','created':time.time()}
            if operation=='restore':metadata['destination']=args.get('destination')
            atomic_json(self.root/(identity+'.json'),metadata)
            self.queue.put_nowait((identity,operation,args))
        return {'id':identity,'state':'queued'}

    def get(self,identity,*,include_result=True):
        if not isinstance(identity,str) or len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):raise ValueError('invalid job ID')
        value=json.loads((self.root/(identity+'.json')).read_text(encoding='utf-8'))
        if include_result and value.get('has_result'):
            value['result']=json.loads((self.root/'results'/(identity+'.json')).read_text(encoding='utf-8'))
        if not include_result:value.pop('result',None)
        if value.get('state')=='running':
            if value.get('operation')=='restore' and value.get('destination'):
                marker=Path(value['destination'])/'RESTORING.json'
                try:value['progress']=json.loads(marker.read_text(encoding='utf-8'))
                except (OSError,ValueError):pass
            elif value.get('operation') in ('capture','media.scan','backup','verify'):
                try:
                    status=json.loads((self.product.root/'status.private.json').read_text(encoding='utf-8'))
                    if status.get('progress'):value['progress']=status['progress']
                except (OSError,ValueError):pass
        return value

    def list(self):
        values=[]
        for path in job_paths(self.product.root)[:40]:
            try:values.append(self.get(path.stem,include_result=False))
            except FileNotFoundError:continue
        return values

    def cancel(self,identity):
        self.get(identity)
        with self.lock:
            event=self.events.get(identity)
            if event:event.set()
        return {'id':identity,'cancel_requested':bool(event)}

    def _run(self):
        from .background import thread_priority
        thread_priority()
        while True:
            item=self.queue.get()
            if item is None:return
            identity,operation,args=item;event=self.events[identity];value=self.get(identity)
            value.update(state='running',started=time.time());atomic_json(self.root/(identity+'.json'),value)
            try:
                if event.is_set():raise InterruptedError('cancelled before starting')
                result=json_value(self.product.execute(operation,args,cancelled=lambda:event.is_set() or self.closing.is_set()))
                atomic_json(self.root/'results'/(identity+'.json'),result);value['has_result']=True
                value['state']='completed'
            except InterruptedError as exc:value.update(state='cancelled',error=str(exc))
            except Exception as exc:
                if event.is_set():value.update(state='cancelled',error='operation cancelled')
                else:
                    value.update(state='failed',error=str(exc))
                    from .health import event as record_event,diagnose
                    record_event(self.product.root,'job_failed',operation=operation,job_id=identity,**diagnose(exc))
            value['finished']=time.time();atomic_json(self.root/(identity+'.json'),value)
            from .health import outcome
            outcome(self.product.root,operation,value['state'],finished=value['finished'],error=value.get('error'),job_id=identity,created=value.get('created'))
            with self.lock:self.events.pop(identity,None)
            self.queue.task_done()
            # Read results are disposable UI cache, not archive evidence.
            reads={'accounts','conversations','messages','timeline','search','observations','media','status','backups'}
            if operation in reads:
                recent=job_paths(self.product.root)
                seen=0
                for path in recent:
                    try:old=json.loads(path.read_text(encoding='utf-8'))
                    except (OSError,ValueError):continue
                    if old.get('operation') not in reads or old.get('state') in ('queued','running'):continue
                    seen+=1
                    if seen>40:
                        path.unlink(missing_ok=True);(self.root/'results'/path.name).unlink(missing_ok=True)

    def close(self):
        self.closing.set()
        with self.lock:
            for event in self.events.values():event.set()
        # The current operation reaches its own checkpoint; never kill a writer.
        self.queue.put(None);self.worker.join()

"""Stable incident identities, quiet retries and persisted important notices."""
from __future__ import annotations
import errno
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from threading import RLock
from urllib.parse import quote
from .config import inside
from .materialize import atomic_json

_lock=RLock()
IMPORTANT_OPERATIONS={'backup','verify','restore','backup.verify','export-portable','archive.start','repository.compact','repository.recover'}
RULES={
 'input':('请检查输入','目标目录或操作参数需要调整，未覆盖已有资料。','查看任务中的具体提示，选择新的目标目录或修正参数。',False),
 'input_busy':('输入正在变化','QQ 正在写入或检查点切换，本次切面未提交，稍后自动重试。','等待自动重试；需要时点击重新检查。',False),
 'freeze_budget':('输入冻结暂缓','本次输入读取超过既有 30 秒保护，已释放读取保护，保留上次成功资料。','保持常规 I/O 调度；负载下降后自动重试，可查看诊断。',False),
 'source_missing':('来源暂不可用','配置的来源文件暂时不存在。','确认原来源磁盘和目录可用，再重新检查。',False),
 'permission':('无法读取来源','系统拒绝访问所需文件。','查看具体路径和权限或占用情况，恢复访问后重新检查。',False),
 'client_version':('QQ 版本已变化','当前客户端版本不符合已经验证的采集绑定，采集已暂缓。','让 Agent 核对客户端版本与绑定；验证兼容性后再采集，不要直接重建旧归档。',True),
 'source_generation':('来源绑定需要核对','来源代际与已有归档绑定不一致，未提交新数据。','让 Agent 核对来源与账号绑定，保留旧资料和备份，不要覆盖重建。',True),
 'authentication':('来源认证未通过','输入页未能通过认证，未提交该切面。','核对已保存的来源绑定和客户端版本，可执行完整校验；不要在聊天中提供密钥。',True),
 'integrity':('完整性检查未通过','文件或记录校验发现不一致。','保留现场，查看诊断；使用完整校验和已验证备份恢复到新目录。',True),
 'disk_full':('磁盘空间不足','归档或备份写入因空间不足未完成。','清理可再生成的临时数据或更换有足够空间的目标，再重试。',True),
 'unexpected':('操作暂未完成','本次操作没有确认完成，已有资料保留。','查看具体原因和诊断，再选择重新检查或相关任务。',False),
}


def diagnose(error):
    message=str(error);lower=message.casefold()
    if any(s in lower for s in ('requires a new destination','requires a fresh separate','destination must','overlap','unknown account','invalid interval','invalid page','unsupported settings','unknown source')):code='input'
    elif 'guard budget' in lower:code='freeze_budget'
    elif any(s in lower for s in ('client version changed','client version unavailable','qq version resource','configured qq client version')):code='client_version'
    elif any(s in lower for s in ('generation changed; explicit','generation/catalog binding','binding mismatch')):code='source_generation'
    elif any(s in lower for s in ('disk is full','disk full','not enough space')) or getattr(error,'errno',None)==errno.ENOSPC or getattr(error,'winerror',None)==112:code='disk_full'
    elif any(s in lower for s in ('hash mismatch','hash differs','integrity','malformed','corrupt','replay differs','bytes differ','owned view changed')):code='integrity'
    elif any(s in lower for s in ('authentication','hmac','invalid saved source key','invalid saved key')):code='authentication'
    elif any(s in lower for s in ('checkpoint is active','database is locked','database table is locked','during capture','during baseline','wal prefix','wal changed','wal grew','wal reset','frozen committed','sharing violation')) or getattr(error,'winerror',None) in (32,33):code='input_busy'
    elif isinstance(error,FileNotFoundError) or 'no such file' in lower:code='source_missing'
    elif isinstance(error,PermissionError) or 'permission denied' in lower or 'access is denied' in lower:code='permission'
    else:code='unexpected'
    title,summary,action,important=RULES[code]
    return {'code':code,'title':title,'summary':summary,'action':action,'immediate':important,'detail':message[:4096]}


def retry_failure(error,previous=None,*,now=None,interval=60):
    now=time.time() if now is None else now;value=diagnose(error);previous=previous or {}
    same=previous.get('code')==value['code']
    count=int(previous.get('count',0))+1 if same else 1
    first=previous.get('first_failure',now) if same else now
    if not isinstance(first,(int,float)) or first>now:first=now
    delay=min(900,max(5,interval)*2**min(count-1,4))
    return value|{'count':count,'first_failure':first,'last_failure':now,'next_retry':now+delay,'retry_seconds':delay}


def event(root,kind,**fields):
    path=inside(root,'ui/health-events.private.json')
    with _lock:
        try:events=json.loads(path.read_text(encoding='utf-8'))['events']
        except (OSError,ValueError,KeyError):events=[]
        item={'sequence':(events[-1].get('sequence',0)+1 if events else 1),'time':time.time(),'kind':kind,**fields}
        try:atomic_json(path,{'schema':'qq-next-health-events/1','events':(events+[item])[-128:]})
        except OSError:return False
        return True


def report(root,config,active,last,jobs,*,now=None,missing_since=None):
    now=time.time() if now is None else now;issues=[];last=last or {}
    if config.get('auto_capture'):
        fatal=last.get('fatal')
        if fatal and not active:
            issues.append(fatal|{'id':'worker:'+fatal['code'],'important':True,'severity':'critical','page':'health'})
        elif not active and missing_since and now-missing_since>=60:
            issues.append({'id':'worker:stopped','code':'worker_stopped','title':'持续归档已意外停止',
              'summary':'设置要求持续归档，但未发现对应的运行写入者。','detail':'请查看最近启动任务与诊断记录。',
              'action':'点击开启持续归档；若启动失败，查看下方具体原因。','important':True,'severity':'critical','page':'health'})
        if active:
            for identity,source in last.get('sources',{}).items():
                for field,component in [('capture_issue','采集'),('media_issue','媒体巡检')]:
                    failure=source.get(field)
                    if not failure and field=='capture_issue' and source.get('state')=='retry' and source.get('error'):
                        failure=retry_failure(source['error'],now=source.get('last_attempt',now))
                    if not failure:continue
                    aged=now-failure.get('first_failure',now)>=1800 and failure.get('count',1)>=3
                    important=bool(failure.get('immediate') or aged)
                    item=failure|{'id':'source:'+identity+':'+component+':'+failure['code'],'source_id':identity,'component':component,
                      'important':important,'severity':'critical' if failure.get('immediate') else 'warning' if aged else 'info','page':'health'}
                    if aged and not failure.get('immediate'):
                        item['title']=component+'长时间未成功';item['summary']='连续重试超过 30 分钟，建议检查来源与当前负载。'
                    issues.append(item)
    outcomes=[]
    for operation in IMPORTANT_OPERATIONS:
        try:outcomes.append(json.loads(inside(root,'ui/operation-outcomes/'+operation+'.json').read_text(encoding='utf-8')))
        except (OSError,ValueError):continue
    maintenance=last.get('maintenance',{})
    if maintenance.get('state')=='error' and not any(o['operation']==maintenance.get('action') for o in outcomes):
        diagnosis=diagnose(maintenance.get('error','维护操作失败'))
        important=diagnosis['code']!='input' and (maintenance.get('action') in ('backup','verify') or diagnosis['immediate'])
        issues.append(diagnosis|{'id':'maintenance:'+maintenance.get('action','unknown')+':'+diagnosis['code'],
          'important':important,'severity':'critical' if important else 'info','operation':maintenance.get('action'),'page':'jobs'})
    # Only the latest outcome of each important operation is unresolved.
    latest={}
    for job in [*jobs,*outcomes]:
        operation=job.get('operation')
        if operation not in IMPORTANT_OPERATIONS:continue
        if job.get('state') in {'queued','running','cancelled'}:continue
        if operation not in latest or job.get('finished',job.get('created',0))>latest[operation].get('finished',latest[operation].get('created',0)):latest[operation]=job
    for operation,job in latest.items():
        if job.get('state') not in {'failed','interrupted'}:continue
        if operation=='archive.start' and active:continue
        diagnosis=diagnose(job.get('error','上次任务未确认完成'))
        important=diagnosis['code']!='input'
        issues.append(diagnosis|{'id':'job:'+operation+':'+diagnosis['code'],'important':important,'severity':'critical' if important else 'info','operation':operation,
          'job_id':job.get('id'),'page':'jobs','title':('恢复任务未完成' if operation=='restore' else diagnosis['title'])})
    for item in issues:item['target']='health/'+quote(item['id'],safe='')
    try:events=json.loads(inside(root,'ui/health-events.private.json').read_text(encoding='utf-8'))['events']
    except (OSError,ValueError,KeyError):events=[]
    return {'issues':issues,'important_count':sum(bool(i['important']) for i in issues),'retry_count':sum(i['severity']=='info' for i in issues),
      'events':events[-40:],'policy':{'ordinary_retries':'silent','same_incident_seconds':21600,'global_seconds':900}}


def next_notice(root,health,*,enabled=True,now=None):
    """Called only by the native shell; browsing status cannot consume notices."""
    if not enabled:return None
    now=time.time() if now is None else now
    important=sorted((i for i in health['issues'] if i['important']),key=lambda i:(i['severity']!='critical',not i.get('immediate',False),i['id']))
    if not important:return None
    path=notice_path(root)
    with _lock:
        try:ledger=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):ledger={}
        sent=ledger.get('sent',{})
        if now-ledger.get('last_sent',0)<900:return None
        fresh=[i for i in important if now-sent.get(i['id'],0)>=21600]
        if not fresh:return None
        # One notice covers all currently visible important issues.
        for item in important:sent[item['id']]=now
        sent=dict(sorted(sent.items(),key=lambda pair:pair[1],reverse=True)[:128])
        atomic_json(path,{'schema':'qq-next-notification-ledger/1','last_sent':now,'sent':sent})
        item=fresh[0]
        return {'id':item['id'],'title':'QQ 归档：'+item['title'],
          'body':item['summary'][:130]+(' 另有重要事项待处理。' if len(important)>1 else '')+' 点击查看原因与处理入口。',
          'target':item['target'],'severity':item['severity']}


def notice_path(root):
    """OS integration state must remain writable when the archive disk is full."""
    home=os.environ.get('QQ_ARCHIVE_NOTIFICATION_HOME')
    if not home and os.name=='nt':home=str(Path(os.environ['LOCALAPPDATA'])/'QQArchiveNext/Notifications')
    if not home:return inside(root,'ui/notification-ledger.private.json')
    identity=hashlib.sha256(str(Path(root).resolve()).upper().encode('utf-8')).hexdigest()[:24]
    return Path(home).resolve()/(identity+'.json')


def outcome(root,operation,state,*,finished=None,error=None,job_id=None,created=None):
    if operation not in IMPORTANT_OPERATIONS or state not in {'completed','failed','interrupted'}:return
    path=inside(root,'ui/operation-outcomes/'+operation+'.json')
    value={'operation':operation,'state':state,'finished':time.time() if finished is None else finished,'created':created or 0}
    if error:value['error']=str(error)[:4096]
    if job_id:value['id']=job_id
    with _lock:
        try:previous=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):previous={}
        if previous.get('finished',0)>value['finished']:return
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            with temporary.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            return True
        except OSError:return False
        finally:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass

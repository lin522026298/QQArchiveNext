from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if stream and hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='QQ 持续归档：本地采集、只读查看、去重备份与新目录恢复')
    parser.add_argument('--state',type=Path)
    parser.add_argument('--report',type=Path,help='把结果写到指定本地 JSON')
    parser.add_argument('--cancel-file',type=Path,help='该本地标记出现时，在安全检查点取消长操作')
    sub=parser.add_subparsers(dest='command')
    sub.add_parser('gui');sub.add_parser('status');sub.add_parser('stop')
    serve=sub.add_parser('serve');serve.add_argument('--stdio',action='store_true');serve.add_argument('--web',action='store_true')
    agent=sub.add_parser('agent');agent_sub=agent.add_subparsers(dest='agent_command',required=True)
    agent_sub.add_parser('capabilities')
    call=agent_sub.add_parser('call');call.add_argument('operation');arguments=call.add_mutually_exclusive_group()
    arguments.add_argument('--arguments',default='{}');arguments.add_argument('--arguments-file',type=Path)
    capture=sub.add_parser('capture');capture.add_argument('--full',action='store_true');capture.add_argument('--no-media',action='store_true')
    sub.add_parser('watch')
    sub.add_parser('verify')
    save=sub.add_parser('backup');save.add_argument('--repository',type=Path,required=True);save.add_argument('--include-keys',action='store_true');save.add_argument('--pin',action='store_true')
    restore=sub.add_parser('restore');restore.add_argument('--repository',type=Path,required=True);restore.add_argument('--destination',type=Path,required=True);restore.add_argument('--point');restore.add_argument('--file-check-only',action='store_true')
    restore.add_argument('--reuse-immutable',action='store_true',help='同卷复用冻结资料以减少空间；无法硬链接时普通复制，全部文件仍校验')
    check=sub.add_parser('verify-backup');check.add_argument('--repository',type=Path,required=True);check.add_argument('--point')
    portable=sub.add_parser('export-portable');portable.add_argument('--repository',type=Path,required=True);portable.add_argument('--destination',type=Path,required=True);portable.add_argument('--point')
    args=parser.parse_args(argv)
    if args.command not in ('restore','verify-backup','export-portable') and not (args.command=='agent' and args.agent_command=='capabilities') and not args.state:parser.error('--state is required')
    try:
        cancelled=lambda:bool(args.cancel_file and args.cancel_file.exists())
        from .config import load
        from .guard import WriterLock
        if args.command in (None,'gui'):
            import subprocess
            from .desktop import application
            shell=application(args.state)/'QQArchiveNext.exe'
            if not shell.is_file():raise ValueError('请先构建桌面外壳；也可用 serve --web 运行本地 Web UI')
            subprocess.Popen([str(shell),'--state',str(args.state),'gui'])
            return 0
        if args.command=='serve':
            from .web import run
            return run(args.state,stdio=args.stdio,browser=args.web)
        if args.command=='agent':
            from .product import Product,capabilities,json_value
            if args.agent_command=='capabilities':result=capabilities()
            else:
                from .web import strict_object
                values=json.loads(args.arguments_file.read_text(encoding='utf-8-sig') if args.arguments_file else args.arguments,object_pairs_hook=strict_object)
                if args.operation not in ('status','settings'):
                    from .background import process_priority
                    process_priority()
                import signal,threading
                stop=threading.Event();previous=signal.signal(signal.SIGINT,lambda *_:stop.set())
                try:result=json_value(Product(args.state).execute(args.operation,values,cancelled=lambda:stop.is_set() or cancelled()))
                finally:signal.signal(signal.SIGINT,previous)
        elif args.command=='status':
            root,config=load(args.state)
            path=root/'status.private.json'
            result=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state':'not_started'}
        elif args.command=='stop':
            from .service import request_stop
            request_stop(args.state);result={'state':'stop_requested'}
        elif args.command in ('capture','watch'):
            from .background import process_priority
            process_priority()
            from .service import Coordinator
            coordinator=Coordinator(args.state);original=coordinator.cancelled
            coordinator.cancelled=lambda:cancelled() or original()
            result=coordinator.run(once=args.command=='capture',force=getattr(args,'full',False),media=not getattr(args,'no_media',False))
        elif args.command=='restore':
            from .backup import restore
            result=restore(args.repository,args.destination,identity=args.point,deep=not args.file_check_only,hardlink_immutable=args.reuse_immutable,cancelled=cancelled)
        elif args.command=='verify-backup':
            from .backup import verify_point
            result=verify_point(args.repository,args.point,cancelled=cancelled)
        elif args.command=='export-portable':
            from .backup_unified import export
            result=export(args.repository,args.destination,identity=args.point,cancelled=cancelled)
        elif args.command=='backup':
            from .backup import create
            with WriterLock(args.state):result=create(args.state,args.repository,include_keys=args.include_keys,pin=args.pin,cancelled=cancelled)
        elif args.command=='verify':
            from .catalog import Catalog
            with WriterLock(args.state):
                root,config=load(args.state);catalog=Catalog(root/'catalog.sqlite')
                try:result=catalog.verify(cancelled=cancelled)
                finally:catalog.close()
                if config['schema']=='qq-next-config/2':
                    from .history import verify
                    result['history']=verify(root,config,cancelled=cancelled)
                if (root/'assets.sqlite').exists():
                    from .media import verify
                    result['media']=verify(root,cancelled=cancelled)
        else:raise ValueError('unknown command')
        if args.report:
            from .materialize import atomic_json
            atomic_json(args.report,result)
        if sys.stdout:print(json.dumps(result,ensure_ascii=False))
        if args.command=='capture' and any(source.get('state')!='ok' for source in result['sources'].values()):return 2
        return 0
    except Exception as exc:
        error={'state':'cancelled' if isinstance(exc,InterruptedError) else 'error','error':str(exc)}
        if args.command=='serve' and args.state:
            try:
                import os,time
                from .config import inside
                from .materialize import atomic_json
                root=Path(args.state).resolve(strict=True)
                atomic_json(inside(root,'ui/host-error.private.json'),{'time':time.time(),'pid':os.getpid(),'error':str(exc)[:4096]})
            except (OSError,ValueError):pass
        if args.report:
            from .materialize import atomic_json
            atomic_json(args.report,error)
        if sys.stderr:print(str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())

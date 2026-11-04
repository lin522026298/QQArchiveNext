"""Loopback-only shared UI, using CaptureNext's local console boundaries."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,unquote,parse_qs

from .config import inside
from .product import Product,Jobs,capabilities,json_value,runtime


def strict_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate JSON key')
        value[key]=item
    return value


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    def __init__(self,host,scope):
        self.host=host;self.scope=scope;self.token=secrets.token_urlsafe(32);self.ticket_key=secrets.token_bytes(32)
        super().__init__(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.server_port)
        self.thread=threading.Thread(target=self.serve_forever,daemon=True);self.thread.start()

    def close(self):
        self.token='';self.ticket_key=secrets.token_bytes(32)
        self.shutdown();self.server_close();self.thread.join()

    def ticket(self,path,mime,filename=None):
        path=Path(path).resolve(strict=True)
        if not path.is_relative_to(self.host.product.root):raise ValueError('media escapes state')
        relative=path.relative_to(self.host.product.root).as_posix()
        value=[relative,mime,str(filename or path.name),int(time.time())+86400]
        encoded=base64.urlsafe_b64encode(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()).decode().rstrip('=')
        signature=hmac.new(self.ticket_key,encoded.encode(),hashlib.sha256).hexdigest()
        return '/media/'+encoded+'/'+signature

    def media_values(self,value):
        if isinstance(value,list):return [self.media_values(v) for v in value]
        if not isinstance(value,dict):return value
        value={k:self.media_values(v) for k,v in value.items()}
        if value.get('path') and 'detected_mime' in value:
            value['url']=self.ticket(value['path'],value['detected_mime'],value.get('original_name'))
        if value.get('voice_path'):value['voice_url']=self.ticket(value['voice_path'],'audio/wav','voice.wav')
        return value


class Handler(BaseHTTPRequestHandler):
    server_version='QQArchiveLocal/1'
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass

    def _headers(self,code,mime,length,*,nonce=None,extra=None):
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'"+(" 'nonce-"+nonce+"'" if nonce else '')+"; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'")
        for key,value in (extra or {}).items():self.send_header(key,value)
        self.end_headers()

    def _send(self,value,code=200):
        payload=json.dumps(json_value(value),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        self._headers(code,'application/json; charset=utf-8',len(payload));self.wfile.write(payload)

    def _route(self,post=False):
        hosts=self.headers.get_all('Host') or []
        if len(hosts)!=1 or hosts[0] not in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)):raise PermissionError('invalid Host')
        origin=self.headers.get_all('Origin') or []
        if len(origin)>1 or (origin and origin[0]!='http://'+hosts[0]) or (post and not origin):raise PermissionError('invalid Origin')
        split=urlsplit(self.path)
        if split.scheme or split.netloc:raise ValueError('absolute request target')
        path=unquote(split.path,errors='strict')
        if '\\' in path or '\0' in path or '..' in Path(path).parts:raise ValueError('invalid path')
        query=parse_qs(split.query,max_num_fields=8,keep_blank_values=True)
        if any(len(v)!=1 for v in query.values()):raise ValueError('duplicate parameter')
        if path.startswith('/api/'):
            tokens=self.headers.get_all('X-QQ-Token') or []
            if len(tokens)!=1 or not self.server.token or not hmac.compare_digest(tokens[0].encode(),self.server.token.encode()):raise PermissionError('API token required')
        return path,query

    def _body(self):
        sizes=self.headers.get_all('Content-Length') or []
        if self.headers.get('Transfer-Encoding') or len(sizes)!=1:raise ValueError('one Content-Length required')
        size=int(sizes[0])
        if size<2 or size>65536:raise ValueError('JSON body limit is 64 KiB')
        if self.headers.get_content_type()!='application/json':raise ValueError('JSON body required')
        value=json.loads(self.rfile.read(size),object_pairs_hook=strict_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('non-finite JSON')))
        if not isinstance(value,dict):raise ValueError('JSON object required')
        return value

    def do_GET(self):
        try:
            path,query=self._route();host=self.server.host
            if path=='/favicon.ico':self._headers(204,'image/x-icon',0);return
            if path=='/api/status':self._send(host.status());return
            if path=='/api/capabilities':self._send(capabilities());return
            if path=='/api/jobs':self._send(host.jobs.list());return
            if path=='/api/job':self._send(self.server.media_values(host.jobs.get(query.get('id',[''])[0])));return
            if path.startswith('/media/'):self._media(path,head=False);return
            if path in ('/','/index.html'):
                nonce=secrets.token_urlsafe(24)
                bootstrap=json.dumps({'token':self.server.token,'scope':self.server.scope},ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
                html=(Path(__file__).parent/'product_web/index.html').read_text(encoding='utf-8')
                payload=html.replace('<!--BOOTSTRAP-->','<script nonce="'+nonce+'">window.QQ_BOOTSTRAP='+bootstrap+';</script>').encode()
                self._headers(200,'text/html; charset=utf-8',len(payload),nonce=nonce);self.wfile.write(payload);return
            static={'/app.js':'text/javascript; charset=utf-8','/styles.css':'text/css; charset=utf-8'}
            if path in static:
                payload=(Path(__file__).parent/'product_web'/path[1:]).read_bytes();self._headers(200,static[path],len(payload));self.wfile.write(payload);return
            self._send({'error':'not found'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except PermissionError as exc:self.close_connection=True;self._send({'error':str(exc)},403)
        except Exception as exc:self.close_connection=True;self._send({'error':str(exc)},400)

    def do_HEAD(self):
        try:
            path,_=self._route()
            if not path.startswith('/media/'):self._headers(404,'text/plain',0);return
            self._media(path,head=True)
        except Exception:self._headers(400,'text/plain',0)

    def do_POST(self):
        try:
            path,_=self._route(post=True);value=self._body();host=self.server.host
            if path=='/api/call':
                self._send(host.jobs.submit(value.get('operation'),value.get('arguments',{})),202);return
            if path=='/api/cancel':self._send(host.jobs.cancel(value.get('id')));return
            if path=='/api/notifications/next' and self.server.scope=='native':
                from .health import next_notice
                status=host.status()
                self._send({'notice':next_notice(host.product.root,status['health'],enabled=status['desktop']['important_notifications'])});return
            if path=='/api/web' and self.server.scope=='native':
                self._send(host.set_web(bool(value.get('enabled'))));return
            if path=='/api/quit' and self.server.scope=='native':
                self._send({'state':'closing'});host.explicit_quit=True;host.stop.set();return
            self._send({'error':'not found'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except PermissionError as exc:self.close_connection=True;self._send({'error':str(exc)},403)
        except Exception as exc:self.close_connection=True;self._send({'error':str(exc)},400)

    def _media(self,path,head):
        parts=path.split('/')
        if len(parts)!=4 or len(parts[2])>8192:raise ValueError('invalid media ticket')
        encoded,signature=parts[2:]
        expected=hmac.new(self.server.ticket_key,encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):raise PermissionError('invalid media signature')
        relative,mime,filename,expires=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if expires<time.time():raise PermissionError('media ticket expired; refresh the page')
        target=inside(self.server.host.product.root,relative)
        allowed={'image/png','image/jpeg','image/gif','image/webp','image/bmp','image/avif','audio/wav','audio/mpeg','audio/ogg','audio/mp4','video/mp4','video/webm','video/quicktime'}
        if mime not in allowed:mime='application/octet-stream'
        with target.open('rb') as stream:
            size=os.fstat(stream.fileno()).st_size;start=0;end=size-1;code=200
            extra={'Accept-Ranges':'bytes'}
            if mime=='application/octet-stream':extra['Content-Disposition']='attachment; filename="media.bin"'
            ranges=self.headers.get_all('Range') or []
            if ranges:
                if len(ranges)!=1:raise ValueError('one byte range required')
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',ranges[0])
                if not match or not any(match.groups()):raise ValueError('invalid byte range')
                left,right=match.groups()
                if left:start=int(left);end=min(end,int(right)) if right else end
                else:start=max(0,size-int(right))
                if start>=size or start>end:
                    self._headers(416,mime,0,extra={'Content-Range':'bytes */'+str(size)});return
                code=206;extra['Content-Range']=f'bytes {start}-{end}/{size}'
            length=max(0,end-start+1);self._headers(code,mime,length,extra=extra)
            if head:return
            stream.seek(start)
            while length:
                block=stream.read(min(1024**2,length))
                if not block:break
                self.wfile.write(block);length-=len(block)


class Host:
    def __init__(self,root,*,web=False,autostart=True):
        self.product=Product(root);self.jobs=Jobs(self.product);self.stop=threading.Event();self.lock=threading.RLock()
        self.explicit_quit=False
        self.native=Server(self,'native');self.browser=None
        import psutil
        from .materialize import atomic_json
        atomic_json(inside(self.product.root,'ui/host.private.json'),{'schema':'qq-next-host/1','state_id':self.product.config['state_id'],
          'pid':os.getpid(),'process_started':psutil.Process().create_time(),'url':self.native.url})
        inside(self.product.root,'ui/host-exit.private.json').unlink(missing_ok=True)
        if web:self.set_web(True)
        if autostart and self.product.config.get('auto_capture') and not runtime(root):self.jobs.submit('archive.start',{})

    def set_web(self,enabled):
        with self.lock:
            if enabled and self.browser is None:self.browser=Server(self,'browser')
            if not enabled and self.browser:
                old=self.browser;self.browser=None;old.close()
            return {'enabled':bool(self.browser),'url':self.browser.url if self.browser else None}

    def status(self):
        value=self.product.execute('status')
        value['web']={'enabled':bool(self.browser),'url':self.browser.url if self.browser else None}
        value['jobs']=self.jobs.list();value['closing']=self.stop.is_set()
        from .health import report
        from .config import load
        _,config=load(self.product.root)
        value['health']=report(self.product.root,config,value['capture'] if value['running'] else None,value['capture'],value['jobs'],missing_since=self.product.missing_since)
        return value

    def close(self,*,stop_capture=True,intentional=True):
        self.stop.set();self.jobs.close()
        # Explicit application exit is safe; ordinary window close never calls this.
        if stop_capture and runtime(self.product.root):
            from .service import request_stop
            request_stop(self.product.root)
            while runtime(self.product.root):time.sleep(.2)
        self.set_web(False);self.native.close()
        inside(self.product.root,'ui/host.private.json').unlink(missing_ok=True)
        import psutil
        from .materialize import atomic_json
        atomic_json(inside(self.product.root,'ui/host-exit.private.json'),{'pid':os.getpid(),'process_started':psutil.Process().create_time(),
          'intentional':intentional,'finished':time.time(),'capture_stop_requested':stop_capture})


def run(root,*,stdio=False,browser=False):
    from .guard import WriterLock
    # Separate host ownership; this never takes the archive writer's lock.
    with WriterLock(inside(Path(root).resolve(),'ui')):
        host=Host(root,web=browser or not stdio)
        try:
            if stdio:
                print(json.dumps({'url':host.native.url,'token':host.native.token}),flush=True)
                def listen():
                    for line in __import__('sys').stdin:
                        if line.strip()=='quit':host.explicit_quit=True;break
                    host.stop.set()
                threading.Thread(target=listen,daemon=True).start()
            else:
                print(json.dumps({'url':host.browser.url,'state':'running'}),flush=True)
            while not host.stop.wait(.5):pass
        except KeyboardInterrupt:host.explicit_quit=True
        finally:host.close(stop_capture=host.explicit_quit or not stdio,intentional=host.explicit_quit or not stdio)
    return 0

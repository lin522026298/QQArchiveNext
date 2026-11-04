"""Headless access to an existing shared host without opening a browser."""
import json
import re
from urllib.request import Request,build_opener,ProxyHandler
from .config import load,inside


def host_call(root,operation,args):
    import psutil
    root,config=load(root)
    info=json.loads(inside(root,'ui/host.private.json').read_text(encoding='utf-8'))
    process=psutil.Process(info['pid'])
    if info.get('state_id')!=config['state_id'] or abs(process.create_time()-info['process_started'])>.05:raise ValueError('shared host is not running')
    url=info['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',url):raise ValueError('invalid local host endpoint')
    client=build_opener(ProxyHandler({}))
    with client.open(url+'/',timeout=10) as response:html=response.read(1024**2).decode('utf-8')
    match=re.search(r'window\.QQ_BOOTSTRAP=(\{[^<]+\});</script>',html)
    if not match:raise ValueError('host bootstrap unavailable')
    boot=json.loads(match.group(1))
    if boot.get('scope')!='native':raise ValueError('wrong host listener')
    headers={'X-QQ-Token':boot['token'],'Origin':url,'Content-Type':'application/json'}
    path,body={
      'jobs':('/api/jobs',None),'job':('/api/job?id='+str(args.get('id','')),None),
      'job.cancel':('/api/cancel',{'id':args.get('id')}),'web':('/api/web',{'enabled':args.get('enabled',True)}),
      'app.quit':('/api/quit',{}),
    }[operation]
    if operation in ('job','job.cancel') and not re.fullmatch(r'[0-9a-f]{32}',str(args.get('id',''))):raise ValueError('invalid job ID')
    data=None if body is None else json.dumps(body).encode()
    with client.open(Request(url+path,data=data,headers=headers),timeout=15) as response:return json.loads(response.read())

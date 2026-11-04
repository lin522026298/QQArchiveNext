"""Owned local state and explicit source generations. No credential discovery."""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from .materialize import atomic_json

SAFE_ID=re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$')


def inside(root,relative):
    return _inside_resolved(Path(root).resolve(),relative)


def _inside_resolved(root,relative):
    """Use one operation's resolved root; still resolve/check each candidate."""
    if not isinstance(relative,str) or not relative or '\\' in relative or ':' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('unsafe relative state path')
    path=(root/relative).resolve()
    if not path.is_relative_to(root):raise ValueError('state path escapes owner')
    return path


def load(root,*,allow_restoring=False,allow_building=False):
    root=Path(root).resolve(strict=True)
    if (root/'RESTORING.json').exists() and not allow_restoring:raise ValueError('restore is incomplete; inspect the restore receipt before opening')
    if (root/'BUILDING.json').exists() and not allow_building:raise ValueError('archive integration is incomplete')
    owner=json.loads((root/'OWNER.json').read_text(encoding='utf-8'))
    config=json.loads((root/'config.private.json').read_text(encoding='utf-8'))
    if owner.get('schema')!='qq-next-state/1' or config.get('schema') not in {'qq-next-config/1','qq-next-config/2'} or config.get('state_id')!=owner.get('id'):
        raise ValueError('state/config owner mismatch')
    uuid.UUID(owner['id'])
    sources=config.get('sources')
    if not isinstance(sources,list) or not sources:raise ValueError('no configured sources')
    seen=set()
    for source in sources:
        identity=source.get('id','');account=source.get('account_key','')
        if not SAFE_ID.fullmatch(identity) or identity in seen or not SAFE_ID.fullmatch(account):raise ValueError('invalid/duplicate source or account ID')
        seen.add(identity)
        supplied=Path(source['database']);database=supplied.resolve()
        if not supplied.is_absolute() or database.is_relative_to(root) or root.is_relative_to(database.parent):
            raise ValueError('source and owned state must be separate')
        inside(root,source['key_file'])
        if 'media_root' in source:
            media=Path(source['media_root']).resolve()
            if media.is_relative_to(root) or root.is_relative_to(media):raise ValueError('media source/state overlap')
    if config['schema']=='qq-next-config/2':
        legacy_seen=set();accounts={s['account_key'] for s in sources}
        for source in config.get('legacy_sources',[]):
            identity=source.get('id','')
            if not SAFE_ID.fullmatch(identity) or identity in seen or identity in legacy_seen or source.get('account_key') not in accounts:
                raise ValueError('invalid unified legacy source binding')
            legacy_seen.add(identity)
            inside(root,source['manifest']);inside(root,source['message_store'])
        for package in config.get('history_packages',[])+config.get('recovery_packages',[]):
            inside(root,package['root']);inside(root,package['inventory'])
        if not allow_building:
            for source in config.get('legacy_sources',[]):
                for name in ('manifest','message_store'):
                    if not any(inside(root,source[name]).is_relative_to(inside(root,p['root'])) for p in config.get('history_packages',[])):
                        raise ValueError('historic dependency has no complete backup inventory')
        if config.get('application_directory'):inside(root,config['application_directory'])
    for name,default,minimum,maximum in [('poll_seconds',3,1,300),('capture_seconds',60,5,86400),('full_verify_seconds',3600,30,604800),('media_seconds',1800,10,86400),('backup_seconds',3600,60,604800)]:
        value=config.setdefault(name,default)
        if not isinstance(value,(int,float)) or not minimum<=value<=maximum:raise ValueError('invalid interval: '+name)
    return root,config


def key_bytes(root,source):
    value=inside(root,source['key_file']).read_bytes().strip()
    if not value or len(value)>4096:raise ValueError('invalid saved source key file')
    return value


def publish(root,config):
    """No counts are guessed in manifests; the live reader owns the counts."""
    accounts={}
    for source in config['sources']:
        account=source['account_key']
        accounts.setdefault(account,source.get('account_label') or account)
    atomic_json(root/'private/account_labels.private.json',{'schema':'qq-account-labels-private/1',
      'accounts':[{'account_key':account,'account_label':label} for account,label in accounts.items()]})
    entries=[]
    unified=config.get('schema')=='qq-next-config/2'
    for account,label in accounts.items():
        name='viewer-'+account+'.json'
        viewer={'schema':'qq-next-viewer/2' if unified else 'qq-next-viewer/1','account_key':account,'account_id':account,'catalog':'catalog.sqlite'}
        if unified:viewer['legacy_sources']=[s for s in config.get('legacy_sources',[]) if s['account_key']==account]
        atomic_json(root/name,viewer)
        entries.append({'account_key':account,'archive_manifest':name})
    # Match the existing publication parser's active schema.
    atomic_json(root/'viewer_manifest.json',{'schema':'qq-next-publication/2' if unified else 'qq-next-publication/1','status':'active',
      'accounts':entries,'private':{'account_labels_sidecar':'private/account_labels.private.json'},'state_id':config['state_id']})


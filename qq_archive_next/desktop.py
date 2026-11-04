"""Per-install desktop preferences and optional, current-user login startup."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from .config import load,inside
from .materialize import atomic_json

RUN_KEY=r'Software\Microsoft\Windows\CurrentVersion\Run'
DEFAULTS={'start_in_tray':True,'close_to_tray':True,'important_notifications':True}


def application(root):
    _,config=load(root)
    candidates=[]
    if getattr(sys,'frozen',False):candidates.append(Path(sys.executable).parents[2])
    if config.get('application_directory'):candidates.append(inside(root,config['application_directory']))
    for folder in candidates:
        if (folder/'QQArchiveNext.exe').is_file() and (folder/'backend/QQArchiveCore/QQArchiveCore.exe').is_file():return folder.resolve()
    raise ValueError('找不到完整的桌面程序目录，请使用已验收的程序包。')


def startup_name(root):
    identity=hashlib.sha256(str(Path(root).resolve()).upper().encode('utf-8')).hexdigest()[:24]
    return 'QQArchiveNext.'+identity


def startup_command(root):
    return subprocess.list2cmdline([str(application(root)/'QQArchiveNext.exe'),'--state',str(Path(root).resolve()),'--login-start'])


def _run_value(root):
    if os.name!='nt':return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as key:return winreg.QueryValueEx(key,startup_name(root))[0]
    except FileNotFoundError:return None


def settings(root,changes=None):
    root,_=load(root);path=inside(root,'ui/desktop.private.json')
    try:stored=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):stored={}
    values={name:stored.get(name) if isinstance(stored.get(name),bool) else default for name,default in DEFAULTS.items()}
    if changes is not None:
        if not isinstance(changes,dict) or set(changes)-set(DEFAULTS)-{'launch_at_login'}:raise ValueError('不支持的桌面设置')
        if any(not isinstance(value,bool) for value in changes.values()):raise ValueError('桌面设置必须为布尔值')
        previous_run=_run_value(root)
        if 'launch_at_login' in changes:
            if os.name!='nt':raise ValueError('登录自启仅支持 Windows')
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as key:
                if changes['launch_at_login']:winreg.SetValueEx(key,startup_name(root),0,winreg.REG_SZ,startup_command(root))
                else:
                    try:winreg.DeleteValue(key,startup_name(root))
                    except FileNotFoundError:pass
        values.update({k:v for k,v in changes.items() if k in DEFAULTS})
        try:atomic_json(path,{'schema':'qq-next-desktop/1',**values})
        except OSError:
            if 'launch_at_login' in changes:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,RUN_KEY) as key:
                    if previous_run is not None:winreg.SetValueEx(key,startup_name(root),0,winreg.REG_SZ,previous_run)
                    else:
                        try:winreg.DeleteValue(key,startup_name(root))
                        except FileNotFoundError:pass
            raise
    current=_run_value(root)
    try:expected=startup_command(root)
    except ValueError:expected=None
    return values|{'launch_at_login':bool(current and expected and current==expected),'startup_supported':os.name=='nt',
      'startup_entry':startup_name(root),'startup_needs_refresh':bool(current and current!=expected)}


def shortcuts(root,*,directory=None):
    """Create only our two named links, without modifying unrelated shortcuts."""
    root,_=load(root)
    if os.name!='nt':raise ValueError('Windows 快捷方式仅支持 Windows')
    import ctypes
    if directory is None:
        buffer=ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None,0x10,None,0,buffer):raise OSError('无法定位用户桌面')
        directory=Path(buffer.value)
    directory=Path(directory).resolve(strict=True);app=application(root)
    # PowerShell receives exact paths through a UTF-8 JSON file, never code interpolation.
    spec=inside(root,'ui/shortcut-request.private.json')
    items=[]
    for name,extra in [('QQ 本地聊天档案.lnk',[]),('QQ 归档后台运行.lnk',['--tray'])]:
        target=directory/name
        items.append({'path':str(target),'target':str(app/'QQArchiveNext.exe'),
          'arguments':subprocess.list2cmdline(['--state',str(root),*extra]),'working_directory':str(app)})
    atomic_json(spec,{'items':items})
    script=r'''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$spec=Get-Content -LiteralPath $env:QQ_ARCHIVE_SHORTCUT_SPEC -Raw -Encoding UTF8 | ConvertFrom-Json
$shell=New-Object -ComObject WScript.Shell
$result=@()
foreach($item in $spec.items){
 if(Test-Path -LiteralPath $item.path){$old=$shell.CreateShortcut($item.path);if($old.TargetPath -ne $item.target -and [IO.Path]::GetFileName($old.TargetPath) -ne 'QQArchiveNext.exe'){throw '同名快捷方式属于其他程序，未覆盖'}}
 $link=$shell.CreateShortcut($item.path);$link.TargetPath=$item.target;$link.Arguments=$item.arguments;$link.WorkingDirectory=$item.working_directory;$link.IconLocation=$item.target+',0';$link.Description='本地 QQ 聊天档案';$link.Save()
 $read=$shell.CreateShortcut($item.path);$result+=@{path=$item.path;target=$read.TargetPath;arguments=$read.Arguments;working_directory=$read.WorkingDirectory}
}
ConvertTo-Json -InputObject @($result) -Compress
'''
    env=os.environ.copy();env['QQ_ARCHIVE_SHORTCUT_SPEC']=str(spec)
    result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,
      encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW,check=True)
    return {'shortcuts':json.loads(result.stdout)}

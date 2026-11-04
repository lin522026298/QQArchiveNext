// Shared Web UI shell. Lifecycle and tray presentation follow CaptureNext.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace QQArchiveNext {
    static class Data {
        public static readonly JavaScriptSerializer Json=new JavaScriptSerializer { MaxJsonLength=8*1024*1024 };
        public static Dictionary<string,object> Map(object value) { return value as Dictionary<string,object> ?? new Dictionary<string,object>(); }
        public static object Get(object value,string key) { object found;return Map(value).TryGetValue(key,out found)?found:null; }
        public static string Text(object value) { return Convert.ToString(value)??""; }
        public static bool Bool(object value) { return value is bool && (bool)value; }
        public static bool BoolOr(object value,bool fallback) { return value is bool?(bool)value:fallback; }
        public static object[] Items(object value) { return value as object[] ?? new object[0]; }
    }

    public sealed class TrayState {
        public string Text;
        public string Fault;
        public readonly List<string> Items=new List<string>();
        public static TrayState From(Dictionary<string,object> state) {
            var result=new TrayState();
            bool running=Data.Bool(Data.Get(state,"running"));
            string phase=Data.Text(Data.Get(Data.Get(state,"capture"),"state"));
            result.Items.Add(running?"持续归档："+(phase=="idle"?"等待变化":phase):"持续归档：已暂停");
            result.Items.Add("媒体巡检："+Data.Text(Data.Get(Data.Get(state,"settings"),"media_seconds"))+" 秒");
            var faults=new List<string>();
            foreach(var pair in Data.Map(Data.Get(Data.Get(state,"capture"),"sources"))) {
                string health=Data.Text(Data.Get(pair.Value,"state"));
                result.Items.Add(pair.Key+"："+(health=="ok"?"正常":health=="retry"?"正在自动重试":health));
            }
            int active=0;
            foreach(object item in Data.Items(Data.Get(state,"jobs"))) {
                string health=Data.Text(Data.Get(item,"state"));
                string op=Data.Text(Data.Get(item,"operation"));
                if(health=="running"||health=="queued")active++;
            }
            foreach(object item in Data.Items(Data.Get(Data.Get(state,"health"),"issues")))if(Data.Bool(Data.Get(item,"important"))) { faults.Add(Data.Text(Data.Get(item,"id")));result.Items.Add(Data.Text(Data.Get(item,"title"))); }
            faults.Sort(StringComparer.Ordinal);
            result.Items.Add("应用任务："+active+" 项进行中");
            result.Items.Add("浏览器入口："+(Data.Bool(Data.Get(Data.Get(state,"web"),"enabled"))?"已开启":"已关闭"));
            int retries=Convert.ToInt32(Data.Get(Data.Get(state,"health"),"retry_count")??0);
            result.Text="QQ 本地档案 · "+(faults.Count>0?"需要处理":retries>0?"正在自动重试":active>0?"任务运行中":running?"归档运行中":"归档已暂停");
            if(result.Text.Length>63)result.Text=result.Text.Substring(0,63);
            result.Fault=String.Join("|",faults.ToArray());
            return result;
        }
    }

    sealed class Shell : ApplicationContext {
        readonly string root;
        readonly EventWaitHandle openSignal,exitSignal;
        readonly NotifyIcon tray;
        readonly Icon appIcon;
        readonly ToolStripMenuItem health=new ToolStripMenuItem("运行状态");
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer { Interval=1000 };
        readonly HttpClient http;
        readonly Process backend;
        string endpoint,token,webUrl="",healthKey="",noticeTarget="health";
        bool closeToTray=true,backendFailed;
        public bool RestartRequested { get; private set; }
        Form window;WebView2 view;
        bool quitting,polling;
        int ticks;
        Dictionary<string,object> latest=new Dictionary<string,object>();
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window,int command);

        public Shell(string state,EventWaitHandle open,EventWaitHandle exit,bool hidden) {
            root=state;openSignal=open;exitSignal=exit;
            http=new HttpClient(new HttpClientHandler { UseProxy=false }) { Timeout=TimeSpan.FromSeconds(10) };
            string core=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,@"backend\QQArchiveCore\QQArchiveCore.exe");
            var info=new ProcessStartInfo(core,"--state "+Quote(root)+" serve --stdio") {
                WorkingDirectory=AppDomain.CurrentDomain.BaseDirectory,UseShellExecute=false,CreateNoWindow=true,
                RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8
            };
            backend=Process.Start(info);
            try {
            backend.ErrorDataReceived+=(s,e)=>{};backend.BeginErrorReadLine();
            var lineTask=backend.StandardOutput.ReadLineAsync();
            if(!lineTask.Wait(20000))throw new IOException("后台启动超时");
            var boot=Data.Map(Data.Json.DeserializeObject(lineTask.Result??"{}"));
            endpoint=Data.Text(Data.Get(boot,"url"));token=Data.Text(Data.Get(boot,"token"));
            Uri uri;
            if(!Uri.TryCreate(endpoint,UriKind.Absolute,out uri)||uri.Host!="127.0.0.1"||uri.Scheme!="http"||token.Length<32)throw new IOException("后台启动失败");
            }catch { try{backend.StandardInput.Close();}catch{}backend.Dispose();http.Dispose();throw; }
            // Drain the pipe; bootstrap credentials never enter a URL or log.
            Task.Run(async()=>{while(await backend.StandardOutput.ReadLineAsync()!=null){};});
            http.DefaultRequestHeaders.Add("X-QQ-Token",token);http.DefaultRequestHeaders.Add("Origin",endpoint);
            var menu=new ContextMenuStrip();
            menu.Items.Add("打开聊天档案",null,async(s,e)=>await OpenWindow("overview"));
            menu.Items.Add("开启持续归档",null,async(s,e)=>await Call("archive.start"));
            menu.Items.Add("安全暂停归档",null,async(s,e)=>await Call("archive.stop"));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("开启并打开浏览器界面",null,async(s,e)=>await Web(true,true));
            menu.Items.Add("关闭浏览器入口",null,async(s,e)=>await Web(false,false));
            menu.Items.Add("备份与恢复",null,async(s,e)=>await OpenWindow("backups"));
            menu.Items.Add("后台检查设置",null,async(s,e)=>await OpenWindow("settings"));
            menu.Items.Add("状态、原因与处理",null,async(s,e)=>await OpenWindow("health"));
            menu.Items.Add(health);menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("安全退出整个工具",null,async(s,e)=>await Quit());
            appIcon=Icon.ExtractAssociatedIcon(Application.ExecutablePath)??SystemIcons.Application;
            tray=new NotifyIcon { Icon=appIcon,Text="QQ 本地档案 · 正在启动",ContextMenuStrip=menu,Visible=true };
            tray.DoubleClick+=async(s,e)=>await OpenWindow("overview");
            tray.BalloonTipClicked+=async(s,e)=>await OpenWindow(noticeTarget);
            timer.Tick+=async(s,e)=>{
                if(quitting)return;
                if(exitSignal.WaitOne(0)) { await Quit();return; }
                if(openSignal.WaitOne(0))await OpenWindow("overview");
                if(backend.HasExited) {
                    if(ShellFiles.IntentionalExit(root,backend)){await Quit();return;}
                    if(!backendFailed){backendFailed=true;tray.Text="QQ 本地档案 · 界面后台已退出";health.DropDownItems.Clear();
                        health.DropDownItems.Add("后台退出码："+backend.ExitCode);health.DropDownItems.Add("点击打开处理入口",null,async(a,b)=>await OpenWindow("health"));
                        if(ShellFiles.ClaimNotice(root,"ui:backend_stopped",DateTimeOffset.UtcNow.ToUnixTimeSeconds()))tray.ShowBalloonTip(6000,"QQ 归档：界面后台已退出","点击查看原因并重新启动界面。",ToolTipIcon.Error);
                        WriteState("backend-stopped");}
                    return;
                }
                if(ticks++%5==0)await Poll();
            };
            timer.Start();WriteState("tray");
            if(!hidden)OpenWindow("overview").ContinueWith(task=>{});
        }
        static string Quote(string value) { return "\""+value.Replace("\"","")+"\""; }
        async Task<Dictionary<string,object>> Post(string path,object value) {
            using(var response=await http.PostAsync(endpoint+path,new StringContent(Data.Json.Serialize(value),Encoding.UTF8,"application/json"))) {
                response.EnsureSuccessStatusCode();return Data.Map(Data.Json.DeserializeObject(await response.Content.ReadAsStringAsync()));
            }
        }
        async Task Call(string operation) {
            bool failed=false;
            try { await Post("/api/call",new { operation=operation,arguments=new {} });await Poll(); }
            catch { failed=true; }
            if(failed)await OpenWindow("health");
        }
        async Task Web(bool enabled,bool reveal) {
            bool failed=false;
            try {
                var result=await Post("/api/web",new { enabled=enabled });webUrl=Data.Text(Data.Get(result,"url"));
                if(reveal && enabled && webUrl!="")Process.Start(new ProcessStartInfo(webUrl) { UseShellExecute=true });
                await Poll();
            }catch { failed=true; }
            if(failed)await OpenWindow("settings");
        }
        async Task Poll() {
            if(polling||quitting)return;polling=true;
            try {
                using(var response=await http.GetAsync(endpoint+"/api/status")) {
                    response.EnsureSuccessStatusCode();latest=Data.Map(Data.Json.DeserializeObject(await response.Content.ReadAsStringAsync()));
                }
                webUrl=Data.Text(Data.Get(Data.Get(latest,"web"),"url"));
                closeToTray=Data.BoolOr(Data.Get(Data.Get(latest,"desktop"),"close_to_tray"),true);
                var value=TrayState.From(latest);tray.Text=value.Text;tray.Icon=value.Fault!=""?SystemIcons.Warning:appIcon;
                string key=String.Join("\n",value.Items.ToArray());
                if(key!=healthKey) {
                    while(health.DropDownItems.Count>0) { var old=health.DropDownItems[0];health.DropDownItems.RemoveAt(0);old.Dispose(); }
                    foreach(string item in value.Items)health.DropDownItems.Add(item,null,async(s,e)=>await OpenWindow("health"));
                    healthKey=key;
                }
                var claimed=await Post("/api/notifications/next",new {});var notice=Data.Map(Data.Get(claimed,"notice"));
                if(notice.Count>0) {
                    noticeTarget=Data.Text(Data.Get(notice,"target"));if(!noticeTarget.StartsWith("health",StringComparison.Ordinal))noticeTarget="health";
                    tray.ShowBalloonTip(6000,Data.Text(Data.Get(notice,"title")),Data.Text(Data.Get(notice,"body")),ToolTipIcon.Warning);
                }
                WriteState(window==null?"tray":"window");
            }catch { tray.Text="QQ 本地档案 · 等待后台响应"; }
            finally { polling=false; }
        }
        void WriteState(string phase) {
            try {
                string folder=Path.Combine(root,"ui");Directory.CreateDirectory(folder);
                string path=Path.Combine(folder,"shell.private.json");string temporary=path+".tmp";
                var value=new { schema="qq-next-shell/1",pid=Process.GetCurrentProcess().Id,backend_pid=backend.Id,phase=phase,
                    window_open=window!=null,url=endpoint,web_url=webUrl,tooltip=tray.Text,health_items=healthKey.Split('\n'),notification_target=noticeTarget };
                File.WriteAllText(temporary,Data.Json.Serialize(value),new UTF8Encoding(false));
                if(File.Exists(path))File.Replace(temporary,path,null);else File.Move(temporary,path);
            }catch { }
        }
        async Task OpenWindow(string page) {
            if(quitting)return;
            if(backend.HasExited) {
                using(var problem=new ProblemForm("界面后台已退出","退出码："+backend.ExitCode+"。可以重新连接现有归档；查看最近诊断记录以定位原因。",root)) {
                    if(problem.ShowDialog()==DialogResult.Retry){RestartRequested=true;await Quit();}
                }
                return;
            }
            if(window!=null) { Reveal(window);if(view!=null&&view.CoreWebView2!=null)view.CoreWebView2.Navigate(endpoint+"/#"+page);return; }
            var form=new Form { Text="QQ 本地聊天档案",Width=1320,Height=880,MinimumSize=new Size(680,520),StartPosition=FormStartPosition.CenterScreen,AutoScaleMode=AutoScaleMode.Dpi };
            form.Icon=appIcon;
            window=form;var browser=new WebView2 { Dock=DockStyle.Fill };view=browser;form.Controls.Add(browser);
            form.FormClosing+=async(s,e)=>{if(!quitting&&!closeToTray&&e.CloseReason==CloseReason.UserClosing){e.Cancel=true;await Quit();}};
            form.FormClosed+=(s,e)=>{browser.Dispose();if(window==form){window=null;view=null;WriteState("tray");}};
            Reveal(form);WriteState("window");
            try {
                var options=new CoreWebView2EnvironmentOptions("--disk-cache-size=1048576 --media-cache-size=1048576");options.IsCustomCrashReportingEnabled=true;
                var env=await CoreWebView2Environment.CreateAsync(null,Path.Combine(root,@"ui\webview-profile"),options);
                if(form.IsDisposed)return;await browser.EnsureCoreWebView2Async(env);if(form.IsDisposed)return;
                var settings=browser.CoreWebView2.Settings;settings.AreDevToolsEnabled=false;settings.AreDefaultContextMenusEnabled=false;settings.IsStatusBarEnabled=false;settings.IsPasswordAutosaveEnabled=false;settings.IsGeneralAutofillEnabled=false;
                browser.CoreWebView2.NavigationStarting+=(s,e)=>{Uri uri;if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out uri)||uri.GetLeftPart(UriPartial.Authority)!=endpoint)e.Cancel=true;};
                browser.CoreWebView2.NewWindowRequested+=(s,e)=>{e.Handled=true;if(webUrl!=""&&e.Uri.StartsWith(webUrl+"/",StringComparison.Ordinal))Process.Start(new ProcessStartInfo(e.Uri){UseShellExecute=true});};
                browser.CoreWebView2.PermissionRequested+=(s,e)=>{e.State=CoreWebView2PermissionState.Deny;};
                browser.CoreWebView2.DownloadStarting+=(s,e)=>{Uri uri;if(!Uri.TryCreate(e.DownloadOperation.Uri,UriKind.Absolute,out uri)||uri.GetLeftPart(UriPartial.Authority)!=endpoint||!uri.AbsolutePath.StartsWith("/media/",StringComparison.Ordinal))e.Cancel=true;};
                browser.Source=new Uri(endpoint+"/#"+page);
            }catch { if(!form.IsDisposed){browser.Dispose();form.Controls.Add(new Label { Text="独立窗口需要 WebView2 Runtime。后台仍可运行，可从托盘开启浏览器界面。",Dock=DockStyle.Fill,Padding=new Padding(25) });} }
        }
        void Reveal(Form form) { form.Show();ShowWindow(form.Handle,9);form.Activate(); }
        async Task Quit() {
            if(quitting)return;quitting=true;timer.Stop();tray.Text="QQ 本地档案 · 正在安全收尾";
            if(window!=null)window.Close();WriteState("closing");
            try { if(!backend.HasExited){backend.StandardInput.WriteLine("quit");backend.StandardInput.Flush();await Task.Run(()=>backend.WaitForExit());} }
            finally { WriteState("stopped");tray.Visible=false;tray.Dispose();appIcon.Dispose();http.Dispose();backend.Dispose();ExitThread(); }
        }
    }

    static class Program {
        static string Argument(string value) {
            var result=new StringBuilder("\"");int slashes=0;
            foreach(char c in value) {
                if(c=='\\') { slashes++;continue; }
                if(c=='\"') { result.Append('\\',slashes*2+1);result.Append(c);slashes=0;continue; }
                result.Append('\\',slashes);slashes=0;result.Append(c);
            }
            result.Append('\\',slashes*2);result.Append('"');return result.ToString();
        }
        [STAThread] static int Main(string[] args) {
            string root=null,portable=null,startupError=null;bool tray=false,quit=false,login=false,isGui=true;
            for(int i=0;i<args.Length;i++) {
                if((args[i]=="--state"||args[i]=="--report")&&i+1<args.Length){if(args[i]=="--state")root=Path.GetFullPath(args[i+1]);i++;}
                else if(args[i]=="--portable-root"&&i+1<args.Length)portable=Path.GetFullPath(args[++i]);
                else if(args[i]=="--login-start")login=true;
                else if(args[i]=="--tray")tray=true;else if(args[i]=="--quit")quit=true;else if(args[i]!="gui")isGui=false;
            }
            if(!isGui) {
                string core=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,@"backend\QQArchiveCore\QQArchiveCore.exe");
                var quoted=new List<string>();foreach(string value in args)quoted.Add(Argument(value));
                try { using(var process=Process.Start(new ProcessStartInfo(core,String.Join(" ",quoted.ToArray())) { UseShellExecute=false,CreateNoWindow=true })) { process.WaitForExit();return process.ExitCode; } }
                catch { return 2; }
            }
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            try {
                var plan=LaunchPlan.Resolve(AppDomain.CurrentDomain.BaseDirectory,root,portable);
                root=plan.State;
                if(plan.RequiresRestore) {
                    string recoveryIdentity=ShellFiles.Identity(plan.Repository);bool first;
                    using(var mutex=new Mutex(true,@"Local\QQArchiveNext.Recovery."+recoveryIdentity,out first))
                    using(var signal=new EventWaitHandle(false,EventResetMode.AutoReset,@"Local\QQArchiveNext.Recovery.Open."+recoveryIdentity)) {
                        if(!first){signal.Set();return 0;}
                        try { using(var form=new PortableRecoveryForm(AppDomain.CurrentDomain.BaseDirectory,plan.Repository,signal)){form.ShowDialog();root=form.SelectedState;} }
                        finally { mutex.ReleaseMutex(); }
                    }
                    if(root==null)return 0;
                }
                if(root==null) {
                    if(login||tray||quit)return 2;
                    using(var picker=new FolderBrowserDialog{Description="选择已有 QQ 归档目录，或完整迁移包目录",ShowNewFolderButton=false}) {
                        if(picker.ShowDialog()!=DialogResult.OK)return 0;
                        if(ShellFiles.IsState(picker.SelectedPath))root=ShellFiles.Full(picker.SelectedPath);
                        else {var selected=LaunchPlan.Resolve(AppDomain.CurrentDomain.BaseDirectory,null,picker.SelectedPath);
                            if(selected.RequiresRestore){Process.Start(new ProcessStartInfo(Application.ExecutablePath,ShellFiles.Arguments("--portable-root",picker.SelectedPath)){UseShellExecute=false});return 0;}
                            root=selected.State;}
                    }
                }
            }catch(Exception exc) {
                if((login||tray||quit)&&root!=null)startupError=exc.Message;
                else {MessageBox.Show(exc.Message,"QQ 档案启动未完成",MessageBoxButtons.OK,MessageBoxIcon.Information);return 2;}
            }
            root=ShellFiles.Full(root);
            if(login)tray=ShellFiles.Preference(root,"start_in_tray",true);
            string identity=ShellFiles.Identity(root);bool restart=false;
            bool created;
            using(var mutex=new Mutex(true,@"Local\QQArchiveNext."+identity,out created))
            using(var open=new EventWaitHandle(false,EventResetMode.AutoReset,@"Local\QQArchiveNext.Open."+identity))
            using(var exit=new EventWaitHandle(false,EventResetMode.AutoReset,@"Local\QQArchiveNext.Exit."+identity)) {
                if(!created) { if(quit)exit.Set();else if(!tray)open.Set();return 0; }
                if(quit) { mutex.ReleaseMutex();return 0; }
                try {
                    if(startupError!=null){var failure=new StartupFailureContext(root,startupError,open,exit);Application.Run(failure);restart=failure.RestartRequested;}
                    else {var shell=new Shell(root,open,exit,tray);Application.Run(shell);restart=shell.RestartRequested;}
                }
                catch(Exception exc) {
                    if(login||tray){var failure=new StartupFailureContext(root,exc.Message,open,exit);Application.Run(failure);restart=failure.RestartRequested;}
                    else using(var problem=new ProblemForm("界面启动未完成",exc.Message+"。现有归档和后台不会被覆盖，可查看原因后重新连接。",root))restart=problem.ShowDialog()==DialogResult.Retry;
                }
                finally { mutex.ReleaseMutex(); }
            }
            if(restart)Process.Start(new ProcessStartInfo(Application.ExecutablePath,ShellFiles.Arguments("--state",root,"--tray")){UseShellExecute=false});
            return 0;
        }
    }
}

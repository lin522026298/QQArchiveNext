using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace QQArchiveNext {
    public static class ShellFiles {
        public static string Full(string value) {
            string path=Path.GetFullPath(value);return path.Length>3?path.TrimEnd(Path.DirectorySeparatorChar,Path.AltDirectorySeparatorChar):path;
        }
        public static string Identity(string root) {
            using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Full(root).ToUpperInvariant()))).Replace("-","").Substring(0,24);
        }
        public static string Quote(string value) {
            var result=new StringBuilder("\"");int slashes=0;
            foreach(char c in value) {
                if(c=='\\'){slashes++;continue;}
                if(c=='"'){result.Append('\\',slashes*2+1);result.Append(c);slashes=0;continue;}
                result.Append('\\',slashes);slashes=0;result.Append(c);
            }
            result.Append('\\',slashes*2);result.Append('"');return result.ToString();
        }
        public static string Arguments(params string[] values) { var result=new List<string>();foreach(string value in values)result.Add(Quote(value));return String.Join(" ",result.ToArray()); }
        public static Dictionary<string,object> Read(string path) { return Data.Map(Data.Json.DeserializeObject(File.ReadAllText(path,Encoding.UTF8))); }
        public static bool IsState(string root) {
            try {
                if(File.Exists(Path.Combine(root,"RESTORING.json"))||File.Exists(Path.Combine(root,"BUILDING.json")))return false;
                var owner=Read(Path.Combine(root,"OWNER.json"));var config=Read(Path.Combine(root,"config.private.json"));
                return Data.Text(Data.Get(owner,"schema"))=="qq-next-state/1"&&Data.Text(Data.Get(owner,"id"))==Data.Text(Data.Get(config,"state_id"))&&File.Exists(Path.Combine(root,"catalog.sqlite"));
            }catch{return false;}
        }
        public static bool IsRepository(string root) {
            try { return Data.Text(Data.Get(Read(Path.Combine(root,"OWNER.json")),"schema"))=="qq-next-backup-repository/1"&&File.Exists(Path.Combine(root,"refs.json")); }
            catch{return false;}
        }
        public static string FindState(string app) {
            foreach(string folder in new[]{Path.GetDirectoryName(app),Path.Combine(app,"state"),Path.Combine(Path.GetDirectoryName(app),"Recovered")})if(IsState(folder))return Full(folder);
            return null;
        }
        public static bool Preference(string root,string name,bool fallback) {
            try{return Data.BoolOr(Data.Get(Read(Path.Combine(root,@"ui\desktop.private.json")),name),fallback);}catch{return fallback;}
        }
        public static string UiDirectory(string root,string child) {
            string folder=Path.Combine(root,child);Directory.CreateDirectory(folder);
            if((File.GetAttributes(folder)&FileAttributes.ReparsePoint)!=0)throw new IOException("临时目录不能是目录联接或符号链接。");
            return folder;
        }
        public static bool ClaimNotice(string root,string id,double now) {
            if(!Preference(root,"important_notifications",true))return false;
            try {
                string folder=Environment.GetEnvironmentVariable("QQ_ARCHIVE_NOTIFICATION_HOME");
                if(String.IsNullOrEmpty(folder))folder=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),@"QQArchiveNext\Notifications");
                Directory.CreateDirectory(folder);string path=Path.Combine(folder,Identity(root)+".json");
                var ledger=new Dictionary<string,object>();try{if(File.Exists(path))ledger=Read(path);}catch{}
                var sent=Data.Map(Data.Get(ledger,"sent"));
                object last;double previous=sent.TryGetValue(id,out last)?Convert.ToDouble(last):0;
                if(now-Convert.ToDouble(Data.Get(ledger,"last_sent")??0)<900||now-previous<21600)return false;
                sent[id]=now;var value=new{schema="qq-next-notification-ledger/1",last_sent=now,sent=sent};
                string temp=path+".tmp";File.WriteAllText(temp,Data.Json.Serialize(value),new UTF8Encoding(false));
                if(File.Exists(path))File.Replace(temp,path,null);else File.Move(temp,path);
                return true;
            }catch{return false;}
        }
        public static bool IntentionalExit(string root,Process process) {
            try {
                var value=Read(Path.Combine(root,@"ui\host-exit.private.json"));
                double started=(process.StartTime.ToUniversalTime()-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
                return Convert.ToInt32(Data.Get(value,"pid"))==process.Id&&Math.Abs(Convert.ToDouble(Data.Get(value,"process_started"))-started)<.1&&Data.Bool(Data.Get(value,"intentional"));
            }catch{return false;}
        }
    }

    public sealed class LaunchPlan {
        public string State;
        public string Repository;
        public bool RequiresRestore;
        public static LaunchPlan Resolve(string application,string requested,string repository) {
            application=ShellFiles.Full(application);
            if(requested!=null) {
                requested=ShellFiles.Full(requested);
                if(!ShellFiles.IsState(requested))throw new IOException("未找到完整档案，或这个目录的恢复尚未完成。请选择正确目录，未覆盖已有资料。");
                return new LaunchPlan{State=requested};
            }
            if(repository==null) {
                string found=ShellFiles.FindState(application);
                if(found!=null)return new LaunchPlan{State=found};
                string parent=Path.GetDirectoryName(application);
                if(ShellFiles.IsRepository(parent))repository=parent;
            }
            if(repository!=null) {
                repository=ShellFiles.Full(repository);
                if(!ShellFiles.IsRepository(repository))throw new IOException("请选择完整迁移包或本工具的备份仓库目录。");
                string recovered=Path.Combine(repository,"Recovered");
                return new LaunchPlan{Repository=repository,State=ShellFiles.IsState(recovered)?recovered:null,RequiresRestore=!ShellFiles.IsState(recovered)};
            }
            return new LaunchPlan();
        }
    }

    sealed class ProblemForm : Form {
        public ProblemForm(string title,string reason,string root) {
            Text=title;Width=650;Height=370;StartPosition=FormStartPosition.CenterScreen;MinimumSize=new Size(500,300);
            var layout=new TableLayoutPanel{Dock=DockStyle.Fill,Padding=new Padding(22),ColumnCount=1,RowCount=3};
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,45));layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,48));
            layout.Controls.Add(new Label{Text=title,Font=new Font("Microsoft YaHei UI",14,FontStyle.Bold),Dock=DockStyle.Fill});
            string diagnostic=reason;
            try { string path=Path.Combine(root,@"ui\host-error.private.json");if(File.Exists(path))diagnostic+="\r\n\r\n"+File.ReadAllText(path,Encoding.UTF8); }catch{}
            layout.Controls.Add(new TextBox{Text=diagnostic,Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Fill});
            var buttons=new FlowLayoutPanel{Dock=DockStyle.Fill,FlowDirection=FlowDirection.RightToLeft};
            var restart=new Button{Text="重新连接档案",AutoSize=true,DialogResult=DialogResult.Retry};buttons.Controls.Add(restart);
            var close=new Button{Text="关闭",AutoSize=true,DialogResult=DialogResult.Cancel};buttons.Controls.Add(close);
            layout.Controls.Add(buttons);Controls.Add(layout);AcceptButton=restart;CancelButton=close;
        }
    }

    sealed class StartupFailureContext : ApplicationContext {
        readonly NotifyIcon tray;
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer{Interval=1000};
        readonly string root,reason;
        public bool RestartRequested { get; private set; }
        public StartupFailureContext(string state,string error,EventWaitHandle open,EventWaitHandle exit) {
            root=state;reason=error;
            var menu=new ContextMenuStrip();menu.Items.Add("查看原因与重新连接",null,(s,e)=>ShowProblem());
            menu.Items.Add("安全暂停归档并退出",null,async(s,e)=>await StopArchive());
            menu.Items.Add("关闭此提示（保留已有后台）",null,(s,e)=>Finish());
            tray=new NotifyIcon{Icon=SystemIcons.Warning,Text="QQ 档案 · 界面启动需要处理",Visible=true,ContextMenuStrip=menu};
            tray.DoubleClick+=(s,e)=>ShowProblem();tray.BalloonTipClicked+=(s,e)=>ShowProblem();
            timer.Tick+=(s,e)=>{if(open.WaitOne(0))ShowProblem();if(exit.WaitOne(0))Finish();};timer.Start();
            try {
                if(!ShellFiles.IsState(root))throw new IOException("档案目录暂不可用，不在原路径创建新档案。");
                string folder=ShellFiles.UiDirectory(root,"ui");string path=Path.Combine(folder,"shell.private.json");
                File.WriteAllText(path,Data.Json.Serialize(new{schema="qq-next-shell/1",pid=Process.GetCurrentProcess().Id,phase="startup-error",window_open=false,error=reason}),new UTF8Encoding(false));
            }catch{}
            if(ShellFiles.ClaimNotice(root,"ui:startup_failed",DateTimeOffset.UtcNow.ToUnixTimeSeconds()))tray.ShowBalloonTip(6000,"QQ 归档：界面启动未完成","点击查看具体原因和重新连接入口。已有资料保留。",ToolTipIcon.Error);
        }
        void ShowProblem() { using(var form=new ProblemForm("界面启动未完成",reason+"。请核对程序是否完整及档案目录是否可用；可重新连接已有后台。",root))if(form.ShowDialog()==DialogResult.Retry){RestartRequested=true;Finish();} }
        async Task StopArchive() {
            try {
                string core=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,@"backend\QQArchiveCore\QQArchiveCore.exe");
                using(var process=Process.Start(new ProcessStartInfo(core,ShellFiles.Arguments("--state",root,"agent","call","archive.stop")){UseShellExecute=false,CreateNoWindow=true})) {
                    await Task.Run(()=>process.WaitForExit());if(process.ExitCode!=0)throw new IOException("后台没有确认停止，请让 Agent 查看诊断。未强制结束任何写入进程。");
                }
                Finish();
            }catch(Exception exc){using(var form=new ProblemForm("停止未确认",exc.Message,root))form.ShowDialog();}
        }
        void Finish() {timer.Stop();timer.Dispose();tray.Visible=false;tray.Dispose();ExitThread();}
    }

    sealed class PortableRecoveryForm : Form {
        readonly string app,repo;
        readonly TextBox destination=new TextBox{Dock=DockStyle.Fill};
        readonly Label status=new Label{Dock=DockStyle.Fill,AutoSize=false};
        readonly TextBox details=new TextBox{Dock=DockStyle.Fill,Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical};
        readonly CheckBox reuse=new CheckBox{Text="同盘复用冻结文件以节省空间；跨盘自动普通复制",Checked=true,AutoSize=true,Dock=DockStyle.Fill};
        readonly Button restore=new Button{Text="恢复并完整验证",AutoSize=true};
        readonly Button verify=new Button{Text="验证迁移包",AutoSize=true};
        readonly Button open=new Button{Text="打开已有档案",AutoSize=true};
        readonly Button cancel=new Button{Text="取消当前任务",AutoSize=true,Enabled=false};
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer{Interval=500};
        readonly EventWaitHandle signal;
        bool busy,closeAfter;
        string cancelPath,phase="";
        public string SelectedState { get; private set; }
        public PortableRecoveryForm(string application,string repository,EventWaitHandle openSignal) {
            app=application;repo=repository;signal=openSignal;
            Text="QQ 本地聊天档案 · 便携恢复";Width=800;Height=600;MinimumSize=new Size(640,500);StartPosition=FormStartPosition.CenterScreen;
            var layout=new TableLayoutPanel{Dock=DockStyle.Fill,Padding=new Padding(22),ColumnCount=1,RowCount=8};
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,42));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,65));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,25));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,38));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,35));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,50));layout.RowStyles.Add(new RowStyle(SizeType.Absolute,62));layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            layout.Controls.Add(new Label{Text="恢复完整档案，然后直接打开",Font=new Font("Microsoft YaHei UI",16,FontStyle.Bold),Dock=DockStyle.Fill});
            layout.Controls.Add(new Label{Text="迁移包："+repo+"\r\n恢复包含全部资料和记录校验，可能耗时较长。只写新目录，不覆盖已有档案；无需登录 QQ。",Dock=DockStyle.Fill});
            layout.Controls.Add(new Label{Text="新恢复目录",Dock=DockStyle.Fill});
            destination.Text=File.Exists(Path.Combine(repo,"PORTABLE_RECEIPT.json"))?Path.Combine(repo,"Recovered"):Path.Combine(Path.GetDirectoryName(repo),"QQ-Recovered");layout.Controls.Add(destination);
            layout.Controls.Add(reuse);
            var buttons=new FlowLayoutPanel{Dock=DockStyle.Fill};buttons.Controls.AddRange(new Control[]{verify,restore,open,cancel});layout.Controls.Add(buttons);layout.Controls.Add(status);layout.Controls.Add(details);Controls.Add(layout);
            verify.Click+=async(s,e)=>await Run(false);restore.Click+=async(s,e)=>await Run(true);
            open.Click+=(s,e)=>{string path=destination.Text.Trim();if(!ShellFiles.IsState(path)){status.Text="这个目录尚未完成恢复。请先恢复，或填写已有完整档案的目录。";return;}SelectedState=ShellFiles.Full(path);DialogResult=DialogResult.OK;Close();};
            cancel.Click+=(s,e)=>RequestCancel();
            FormClosing+=(s,e)=>{if(busy){e.Cancel=true;closeAfter=true;RequestCancel();}};
            timer.Tick+=(s,e)=>{if(signal.WaitOne(0)){Show();WindowState=FormWindowState.Normal;Activate();}UpdateProgress();};timer.Start();
            FormClosed+=(s,e)=>timer.Dispose();UpdateReady();
        }
        void UpdateReady() {
            string path=destination.Text.Trim();bool ready=ShellFiles.IsState(path);
            if(ready)status.Text="已有完整档案，可直接打开。";
            else if(File.Exists(Path.Combine(path,"RESTORING.json")))status.Text="上次恢复未完成，原始迁移包保留。请选择一个新的恢复目录；未完成目录不会被覆盖或自动删除。";
            else status.Text="建议先验证迁移包，再恢复到新目录。";
        }
        void RequestCancel() { if(!busy||cancelPath==null)return;File.WriteAllText(cancelPath,"cancel",Encoding.ASCII);cancel.Enabled=false;status.Text="正在等待安全检查点取消；保留未完成标记，请勿强制结束程序。"; }
        void UpdateProgress() {
            if(!busy||phase!="restore")return;
            string marker=Path.Combine(destination.Text.Trim(),"RESTORING.json");
            try { if(File.Exists(marker))details.Text=File.ReadAllText(marker,Encoding.UTF8); }catch{}
        }
        async Task Run(bool recovering) {
            if(busy)return;
            string target;
            try { if(recovering&&(!Path.IsPathRooted(destination.Text.Trim())||Path.GetPathRoot(destination.Text.Trim()).Length<3))throw new IOException("恢复目录必须是绝对路径。");target=recovering?ShellFiles.Full(destination.Text.Trim()):"";if(recovering&&Directory.Exists(target))throw new IOException("目标目录已经存在，请选择新的目录；已有资料不会被覆盖。"); }
            catch(Exception exc){status.Text=exc.Message;return;}
            string folder;
            try { folder=ShellFiles.UiDirectory(ShellFiles.UiDirectory(Path.GetTempPath(),"QQArchiveNext-Recovery"),ShellFiles.Identity(repo)); }
            catch(Exception exc){status.Text=exc.Message;return;}
            string id=Guid.NewGuid().ToString("N"),report=Path.Combine(folder,id+".result.json");cancelPath=Path.Combine(folder,id+".cancel");
            var args=new List<string>{"--report",report,"--cancel-file",cancelPath,recovering?"restore":"verify-backup","--repository",repo};
            if(recovering){args.Add("--destination");args.Add(target);if(reuse.Checked)args.Add("--reuse-immutable");}
            busy=true;phase=recovering?"restore":"verify";restore.Enabled=verify.Enabled=open.Enabled=destination.Enabled=reuse.Enabled=false;cancel.Enabled=true;
            status.Text=recovering?"正在恢复并校验全部资料，可安全取消。":"正在逐文件验证迁移包，可安全取消。";details.Clear();
            try {
                string command=ShellFiles.Arguments(args.ToArray());
                var info=new ProcessStartInfo(Path.Combine(app,@"backend\QQArchiveCore\QQArchiveCore.exe"),command){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
                using(var process=Process.Start(info)) {
                    var output=process.StandardOutput.ReadToEndAsync();var errors=process.StandardError.ReadToEndAsync();await Task.Run(()=>process.WaitForExit());await output;string error=await errors;
                    string result=File.Exists(report)?File.ReadAllText(report,Encoding.UTF8):error;details.Text=result;
                    status.Text=process.ExitCode==0?(recovering?"恢复与完整验证已完成，点击打开档案。":"全部文件验证通过，可以恢复。"):File.Exists(cancelPath)?"操作已取消；未完成的恢复保留标记，可选择新目录重新开始。":"操作未完成，请查看下方原因；原有资料保留。";
                }
            }catch(Exception exc){status.Text="未能完成操作："+exc.Message;}
            finally { busy=false;restore.Enabled=verify.Enabled=open.Enabled=destination.Enabled=reuse.Enabled=true;cancel.Enabled=false;try{File.Delete(cancelPath);}catch{}cancelPath=null;if(closeAfter)Close(); }
        }
    }
}

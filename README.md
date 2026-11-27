# QQArchiveNext · QQ 本地聊天档案

Windows QQ 聊天归档工具：本地持续采集、历史记录统一浏览、搜索与时间线、原始字段和版本保留、媒体关联、完整去重备份和新目录恢复。

桌面 App 与 Web UI 共用界面，GUI 与 Agent 共用后台操作。没有内置大模型；你可以让有本地执行权限的 Agent 使用 JSON CLI，全程无需模拟键鼠。

## 下载与首次使用

从 [Releases](https://github.com/lin522026298/QQArchiveNext/releases) 下载 **QQArchiveNext-1.1.0-Windows-x64.zip** 和 SHA256SUMS.txt，核对 SHA-256 后完整解压。需要 Windows x64、.NET Framework 4.8 和 Microsoft WebView2 Runtime；运行便携程序不需要安装 Python。

**程序包不含任何聊天、账号数据或源密钥。** 双击 QQArchiveNext.exe，可选择已有档案或自己的完整迁移包。已有迁移包的 Start.cmd 可打开图形验证、恢复助手。恢复写新目录，默认执行完整文件和记录校验。

新建采集档案：准备你本人授权的 QQ 数据库、对应的 **原始 16 字节密钥文件**、QQ.exe 和可选 nt_data 媒体目录，然后在解压目录打开 PowerShell：

```powershell
.\Configure-QQArchive.ps1 -StateRoot D:\QQArchive `
  -Database D:\QQData\nt_msg.db -KeyFile D:\LocalSecrets\source.key `
  -ClientExe 'C:\Program Files\Tencent\QQNT\QQ.exe' `
  -MediaRoot D:\QQData\nt_data -AccountAlias account
```

StateRoot 必须是尚不存在的新目录，与原数据和解压程序目录分开。配置工具复制程序、只读验证首次采集，并保留原来源。随后打开 **D:\QQArchive\application\QQArchiveNext.exe**，在界面中开启持续归档。登录自启默认关闭，可在设置中选择开启。

**本版本仅验证 Windows QQ 9.9.19.34467 的输入布局。** 不自动发现密钥、不读取 QQ 进程内存、不代替 QQ 登录。没有对应密钥时不能新建采集；已有完整档案的浏览和恢复不需要 QQ 登录。执行策略拦截脚本时，先检查脚本内容，再按自己的 Windows 策略允许本次脚本运行。

## 日常使用

- 关闭窗口默认留在托盘；“安全退出整个工具”才停止采集。设置中可调整关窗行为、自启、重要通知及巡检间隔。
- 普通重试静默退避，重要故障持久去重限频，点击通知直达具体原因和处理入口。
- 聊天页提供会话、搜索、时间线和日期定位；单条记录可查看来源、原生字段、已观测版本及媒体。
- 备份页可创建、固定及验证备份点，恢复到新目录，导出包含自己资料和恢复材料的完整迁移包。将私人迁移包完整复制到另一台 Windows 电脑后恢复。
- 媒体默认每 30 分钟检查，后台 CPU 较低优先级。耗时随文件数量、源盘和整机状态变化，不承诺固定 30 秒。

完整档案、备份及迁移包可能包含源密钥、私人聊天和媒体，请自行保管，**不要上传到公开仓库或 Release**。该工具保存已成功观测到的本地资料，不承诺手机/云端全部历史、已消失的中间变化或任意未来 QQ 版本。

[使用说明](docs/usage.md) · [Agent 接口](docs/agent.md) · [源码构建](docs/build.md) · [验证与边界](docs/verification.md) · [安全说明](SECURITY.md)

## 开源与来源

GPL-3.0。第三方许可和对应源码随包保留，详见 [第三方来源](qq_archive_next/ORIGIN.md)。与腾讯没有隶属或背书关系；仅用于本人数据或明确获得授权的数据。

## 1.1.0：滚动恢复点

新建仓库默认只保留最近两个成功点和全部固定点，存在退役数据时每七天验证整理一次。需要长期留存的点请先固定。旧仓须显式成功执行一次验证整理才启用滚动保留，安装升级本身不会淘汰旧点。失败或取消的备份保留原可用点。详见 [仓库整理](docs/repository.md)。GUI、CLI 和 Agent 共用 `repository.status`、`repository.compact`、`repository.recover`；切换有落盘日志、互斥锁和崩溃回退，原始资料、版本及附件关联保留。

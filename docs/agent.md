# Agent 后台使用

这是本机执行接口，没有内置 LLM 或云服务。Agent 需要本地命令执行权限，操作前确认用户授权和目标状态目录。读取、备份和恢复不需要登录 QQ；只使用明确配置的来源，不自动发现源密钥。

```powershell
$core='D:\QQArchive\application\backend\QQArchiveCore\QQArchiveCore.exe'
& $core agent capabilities
& $core --state D:\QQArchive --report D:\Reports\status.json agent call status
& $core --state D:\QQArchive agent call health
& $core --state D:\QQArchive agent call backup --arguments-file D:\LocalConfig\backup.json
```

复杂参数使用 UTF-8 JSON 参数文件，不在命令行或对话中传密钥。所有命令支持 --report，输出 JSON；失败返回非零。`--cancel-file <绝对路径>` 指定的本地标记出现后在安全检查点取消；不要强制杀死写入者。

| 操作 | 参数与用途 |
| --- | --- |
| status / health / accounts | 状态、故障及可用账号；先读取标识再操作 |
| conversations | account，可选 query、kind |
| messages | account、conversation，可选 before、after、around、at、limit |
| timeline / search | account；搜索另需 query，可选 limit、offset |
| observations | account、msg_id，原始字段和全部已观测版本 |
| media | account、source_table、msg_id，可选 voice |
| archive.start / archive.stop | 开启或安全暂停持续归档 |
| source.retry | source_id，安排重新检查 |
| capture / media.scan / verify | 可选 full；完整校验保留原有完整性规则 |
| backup / backups | 可选 repository、pin（backup） |
| backup.pin / backup.verify | repository、point、pinned（pin）；逐文件验证可省略 point |
| restore / export-portable | repository、destination，可选 point；恢复可选 reuse_immutable |
| settings | settings 对象：auto_capture、auto_backup、poll_seconds、capture_seconds、media_seconds、full_verify_seconds、backup_seconds |
| desktop.settings | settings 对象：launch_at_login、start_in_tray、close_to_tray、important_notifications |
| desktop.shortcuts | 创建本工具的两个桌面入口并回读实际目标 |
| jobs / job / job.cancel | 共享任务列表、结果或取消，后两项需要 id |
| web / app.quit | enabled 启闭浏览器入口；明确安全退出界面和后台 |

GUI、Web UI 与 CLI 共用 Product 操作。活跃写入者通过运行实例绑定的持久请求箱串行执行维护，不要另开 SQLite 写入者。窗口关闭默认保留后台；app.quit 明确安全退出。

HTTP 仅监听 127.0.0.1，Host/Origin/独立会话令牌与媒体票据必须保留。GET /api/status、/api/capabilities、/api/jobs、/api/job?id=...；POST /api/call（operation、arguments）、/api/cancel（id）。写请求需同源 Origin 和 X-QQ-Token，令牌从本机同源引导取得，不放入 URL、日志或仓库。系统通知领取仅限原生监听器，普通浏览状态不会消耗通知。

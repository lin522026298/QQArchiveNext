# 从源码构建

Windows x64，Python 3.11、Git、.NET Framework C# 编译器。安装固定依赖：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/Build-QqNativeReader.py
```

原生只读 reader 的 SQLite、Zig 来源及 SHA-256 均固定在构建脚本。其 pending-byte 位置修正用于 NTQQ 的 1024 字节封装；运行时验证库哈希、版本和位置，不修改 QQ 或系统 SQLite。

将 Microsoft.Web.WebView2 **1.0.3719.77** 的 NuGet 包放入 `work/webview2.1.0.3719.77.nupkg`。官方包下载地址：
https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.3719.77/microsoft.web.webview2.1.0.3719.77.nupkg

构建脚本核验其 SHA-256 `2f6be3a10a1a8d6d1fde986af4131dab344f8181fffe75590824e0f4b037ed73`。提交源码并保持 Git 工作区干净，然后：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/Build-QqArchiveNext.py --output work/release
.\.venv\Scripts\python.exe scripts/Verify-QqNextRelease.py --release work/release/QQArchiveNext --work work/frozen-test
```

使用尚不存在的输出/测试目录。测试只使用合成资料。发布包保留本提交的源码快照、原生 reader 原始/修正 C 与构建元数据、第三方许可及逐文件哈希。GPL 组件的对应源码已随软件包含，其他依赖使用 requirements.txt 中的固定版本。

只打包 release_verification.json 指定的程序文件和该清单；不要打包测试 work、真实 state、私人配置或整个本机工作目录。公开分支使用审查后的独立历史，不能直接推送原本机个案历史。

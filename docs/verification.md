# 验证与边界

本公开版本由稳定工具的当前源码整理，未携带本机开发历史、个案日志或私人验收数据。公开测试使用合成数据库、账号别名和媒体，覆盖原生行/版本、历史来源、媒体关联、完整备份恢复、取消、单写入者及 GUI/Agent 操作。

发布前检查包括公开源码、Git 历史、程序文件、内嵌 source.zip、冻结 Python 模块和下载回读。Gitleaks 使用默认规则，仅对官方 SQLite C 中四个已核对的函数/常量名误报做精确范围豁免，源码另有固定 SHA-256；不豁免密钥文件或整份代码目录。

下载包会逐文件核对程序清单，并实际执行合成采集、搜索、版本读取、媒体关联、完整备份验证与新目录恢复。另验证配置脚本只写新状态、不改变原输入，已有目录会拒绝覆盖。最终数值与包 SHA-256 见对应 Release 的验证说明。

完整资料备份的范围以已提交的备份点为准。只保存工具成功观测过的本地记录；不承诺恢复未落盘、已消失或未被客户端保存的聊天。普通巡检检查全部候选路径并复用固定根目录解析，完整媒体哈希检查仍可单独执行。当前整机环境中的巡检耗时会变化，不能将某次受控对照中的约 30 秒视为固定保证。

同卷冻结文件复用仍共享字节；跨卷普通复制夹具只证明独立文件恢复，不等于异地或另一实体电脑的全量验收。程序未签名，必须从本仓库 Release 下载并核对校验和。
# 1.1.1 validation

The staged public tree passed 122 tests, including rolling retention, pinned-point preservation, failed/cancelled backups, legacy external-slice independence, corruption handling, shared readers, interrupted generation switches, weekly maintenance and the shared GUI/Agent operations. The program package and anonymous download acceptance are recorded separately in the Release VERIFICATION.json. Tests use synthetic data; no private archive, account, key, path inventory or real message content is published.


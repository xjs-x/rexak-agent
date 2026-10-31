# 聊天与项目归类交付记录

2026-10-28。决策：[0017](../docs/decisions/0017-chat-organization-and-deletion.md)。

## 已交付

- 对话右键与省略号菜单：重命名、删除；删除需确认，并明确不可撤销。
- 项目（包括原“我的工作台”）可重命名；支持新建项目与项目内新聊天。
- 普通新聊天不自动归属项目，数据库 `project_id` 为 NULL。
- 项目仅作归类；各聊天沿用独立 thread、领域记录和会话记忆命名空间。
  新测试身份归属会话，旧身份资料保留；仅允许明确设为 active 的旧身份使用。
- 删除在一个数据库事务内清理会话、业务记录、无外键台账、checkpoint、
  checkpoint heads/write receipts 和会话记忆；不删除其他会话。
- 未结束的运行、作业或未确认关闭的执行资源返回 409，保留管理入口。
  关闭资源请求沿用原执行端协议，关闭请求不等于关闭确认。
- 共享 CAS 文件、技能库共享工件和既有备份不做扫描删除。
- 原桌面入口现在能识别并打开已经运行的本机 Rexak API，避免重复启动；
  其他服务占用端口时仍拒绝复用。

## 验证与限制

- 相关回归 66 passed；最后的定向检查 48 passed；桌面入口相关 19 passed。
- 前端 11 passed，前端构建、Pyright、源码范围 Ruff、UTF-8 无 BOM 检查通过。
- 首轮全量为 1086 passed、12 failed、3 skipped：其中旧删除/旧记忆/旧 schema
  假设及新增字段的旧库只读导出兼容问题已经修正，并通过相关定向回归。
- 全量仍发现资产注册的既有 `uq_assets_endpoint_identity` 约束问题，未扩展修改该模块。
  第二轮全量观察到这 3 项失败；随后按用户“不要做重复性的测试”的要求停止，
  没有完整最终汇总，不声称全量回归通过。没有继续启动重复测试。
- 所有删除测试均在本轮随机命名的隔离 PostgreSQL 数据库运行，未删除日常历史对话。
- 未运行真实模型或靶场效果实验。

## 本机与 Kali

迁移 `e82d934a10bc` 只增列、回填 owner、允许空项目归属；原项目和历史聊天不搬迁。
本机 API 与 Runner 已加载新进程；本次启动使用 `--no-recover`，保留历史恢复入口，
避免启动时重跑旧模型请求。原桌面快捷方式仍指向本仓库 `start-workbench.cmd`，
该入口打开已有新版服务已验收。

Kali 23 个 Python 文件与本机哈希一致，身份/能力匹配，核对时 active_jobs=0；
执行端源码未改，保留原 boot_id 和进程。历史取消中的运行仍保留原状态，未替用户恢复测试。

备份：`data/backups/chat-projects-20261028/20261028T135554918613Z/`，含数据库与工件。
交付前保存的项目、聊天、运行、指令、模型调用、事件、工件、观察、会话图主键集合
在交付后均无缺失；凭据配置文件哈希未变。模型调用总数保持 1467，未自动发出新模型请求。

本地证据（包含运行元数据，不提交）：

- `data/tmp/chat-projects-preservation.json`
- `data/tmp/chat-projects-predeploy.json` 与 `chat-projects-postdeploy.json`
- `data/tmp/chat-projects-regression-final.log` 与 `chat-projects-final-focused.log`
- `data/tmp/chat-projects-web-tests.log`、`chat-projects-entry-tests.log`
- `data/tmp/chat-projects-context-menu.png`

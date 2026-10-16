# 0006 · Python 执行底座与阶段后资源保留

- 日期：2026-10-16。
- 适用：本仓库控制端与指定 Kali Executor。基础模型循环继续使用官方 `create_agent`。

命令、脚本、监视、PTY、屏幕、文件编辑和检索融入现有 Python 架构。执行端新增 `/execution/v1`，旧 HTTP、浏览器与文件接口保留；协议真源在 `contracts/execution_runtime.py`，Executor 使用自动生成的独立副本。新能力由真实预检和 `EXECUTOR_EXECUTION_MODE=trusted` 共同开放，发行默认 disabled。

可信执行不逐条审批命令，不承诺系统级出站隔离或 cwd 沙箱。HTTP、浏览器工具自身的范围规则继续生效；工作区存储目前为 `accounting_only`。

进程、用途与值班是独立维度。`completion_role=work` 表示阶段待办，`resource` 表示需保留的能力；命令启动的监听器可以是后者。`background` 只影响本次等待。日志超额默认保活并继续排空，显式选择 terminate 才终止。资源存活不自动建立持续委托。

非交互任务由独立包装进程持有，服务正常停止或崩溃后继续运行；包装进程负责截止、配额、日志和完成回执。交互终端由服务持有 PTY，重启后允许失去交互能力，必须明确显示；不能伪造接回、重建终端或重发输入。PTY 创建使用 `posix_spawn` 与控制终端，避免在多线程服务中 fork 后运行 Python。

等待到期返回原 job，不重新提交。操作身份在控制端先持久化，执行端独立校验载荷摘要、归属和控制 fence；未知启动窗口不得自动重复。取消要核对整个进程组。阶段结束保留资源；暂停停止 Agent 推进；取消值班结束委托；关闭单个资源、终止整个任务分别具有明确入口。终止任务取消值班并处理同会话保留资源，未确认退出不显示为已停止，也不通过删除对话隐藏资源。

事件持续记录和展示。默认阶段结束不唤醒 Agent；明确的 once/continuous 委托才按条件触发。取消委托递增 revision，旧派发失效，资源不因此关闭。用户暂停期间不自动恢复。下次续聊使用有界聚合摘要和稳定 message ID；只有图 checkpoint 确认持久接收才标记交付，界面已读不代表模型已接收。

真实执行状态归 Executor SQLite；用途、等待、委托与交付意图归业务表；图状态归 checkpoint；长日志与原始字节归工件。日志原始区间可导出为不可变工件，快照 hash 不代表仍在变化的整个日志。没有空闲自动清理，也没有新增任务总时长/费用上限。

详细接口见 [实现规格](../plans/execution-runtime-implementation-spec.md)，实际验证范围见 [工程与部署验收](../acceptance-results/2026-10-16/execution-runtime/README.md)。测试、部署与真实模型/靶场效果分别记账，后者由用户执行。

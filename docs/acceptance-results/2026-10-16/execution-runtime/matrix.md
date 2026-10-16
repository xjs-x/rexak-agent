# 稳定验收 ID 与证据索引

本表沿用[验收清单](../../../plans/execution-runtime-acceptance.md)的 64 个 ID。PASS 指下列实际检查通过；“覆盖边界”限制其证明范围，不能据此声称每种故障排列均已演练。Linux 脚本的 `linux/results.json`仅含其实际执行的 31 项，不覆盖整张表。A01 的一次提交由最终 Git 记录核对，报告不自引用 commit SHA。

简称：L = `linux/results.json`及其来源日志；CT = `final-control-tests.txt`；EL = `all-linux-tests-ported.txt`；EX = `extended-linux-pytest.txt`；UI = `ui/ui-results.json`。测试路径均相对仓库根目录，manifest 提供对应内容哈希。

| ID | 结果 | 具体证据/检查 | 覆盖边界 |
|---|---|---|---|
| A01 | PASS | source-manifest、preserved-user-changes、暂存 diff 检查及最终单次 commit | 用户认证/模型设置等既有改动保留在工作区 |
| A02 | PASS | 生成器 --check；故意改生成副本检查返回非零后恢复；Kali 独立 wheel 导入 | 未用外部项目源码 |
| A03 | PASS | L/A03；test_execution_contracts.py | 正文保真及非法参数前置拒绝 |
| A04 | PASS | CT/test_execution_gating.py、test_execution_tools.py；部署 capabilities | trusted/disabled/旧能力声明与本机无回退 |
| A05 | PASS | OpenAPI 导出、前端 generate:api/typecheck/build | 生成类型为真源 |
| A06 | PASS | EL/test_execution_ledger.py；PG db-init 重复运行及 CT/test_upgrade_preflight.py | SQLite 旧记录/重复冲突、PG 增量升级；降级另见 H06 |
| B01 | PASS | L/B01；EL/test_execution_jobs_linux.py | shell/heredoc/分流/非零退出 |
| B02 | PASS | CT/test_execution_tools.py；EL/test_filesystem_jobs.py | 共享工作区及跨 workspace 拒绝；跨阶段使用相同 session 根 |
| B03 | PASS | L/B03；L/D01；EL/test_terminals_linux.py | 命令 cwd/env 隔离、持久 shell 状态延续 |
| B04 | PASS | L/B01；CT/test_execution_final_controls.py | JSON stdin、解释器解析及摘要；缺解释器由能力/确定失败路径处理 |
| B05 | PASS | EL/test_filesystem_execution.py、test_filesystem_jobs.py | 批量事务、版本复核、原子替换、软链接逃逸 |
| B06 | PASS | EL/test_filesystem_execution.py | 有界 glob/grep、同文件分页、非法编码与正则超时 |
| B07 | PASS | CT/test_template_versions_are_immutable_and_parameters_are_data | 并发首次登记回归、参数数据化、版本不可变 |
| B08 | PASS | L/B08 | 本地 wheel、专属 venv；不访问外网包源 |
| C01 | PASS | L/C01；EL/test_execution_jobs_linux.py | 同 job 等待/后台与单次执行 |
| C02 | PASS | L/C02；L/C09 | wait 与 deadline 分离；无截止不套默认任务总时限 |
| C03 | PASS | L/C03 | 自有服务 SIGTERM 重启，持续日志与核对 |
| C04 | PASS | L/C04 | SIGKILL 服务、停机期间任务退出及真实完成回执 |
| C05 | PASS | L/C05；EX/test_execution_recovery_windows.py | 损坏回执与模拟 PID ticks 不匹配；没有真实操作系统 PID 重用压力试验 |
| C06 | PASS | L/C06；CT/test_execution_final_controls.py | 多个同 ID 首次并发与冲突 409；控制端首次 projection 竞争 |
| C07 | PASS | EX/test_execution_recovery_windows.py | received/claimed/started/completed 四窗口故障注入；不代表穷尽所有时序 |
| C08 | PASS | L/C08；EL/test_processes.py | 实际进程组取消、重复取消及 TERM/KILL 边界 |
| C09 | PASS | L/C09；EL/test_execution_jobs_linux.py | 离线截止、包装进程输出策略；非服务恢复补杀 |
| C10 | PASS | L/C10 | Kali 个人实际控制脚本停止/重启；未宣称 systemd cgroup 或整机重启演练通过 |
| D01 | PASS | L/D01；EL/test_terminals_linux.py | 真 PTY 多轮回显/REPL |
| D02 | PASS | L/D02；EL/test_terminals_input.py | keys/hex/paste 字节与整单校验 |
| D03 | PASS | L/D03；L/D01；L/D04 | 模式时机及 quiet/prompt 与程序退出区分 |
| D04 | PASS | L/D04 | 新输入只匹配新输出；等待到期保留终端 |
| D05 | PASS | L/D05；EX/test_concurrent_inputs_and_write_before_receipt_failure | 真 PTY 并发串行、写后回执失败不重发 |
| D06 | PASS | L/D06；EL/test_terminals_linux.py | ANSI/CR/中文及 resize/screen |
| D07 | PASS | L/D07 | 交互丢失、历史保留、不重建；关闭校验进程身份 |
| D08 | PASS | L/D08；CT/test_execution_api.py；UI | 幂等关闭、分页、跨 session 及 unknown 展示 |
| E01 | PASS | L/E01；EL/test_output_store.py、test_terminal_output_pages.py | 原始字节游标与行数截断边界 |
| E02 | PASS | L/E02 | 原件保留，文本显式编码错误；JSON 中替换字符使用转义保存相同值 |
| E03 | PASS | L/E03 | 超额丢弃、缺失区间、进程保活 |
| E04 | PASS | L/E04（修复后）；EL/test_output_store.py | 初次失败保留，修正索引定期持久化后复验 |
| E05 | PASS | L/E05；EL/test_execution_ledger.py | 五方争抢剩余预算仅一方受理、旧证据保留 |
| E06 | PASS | L/E06 | 当前 EOF 与进程结束分开 |
| E07 | PASS | L/E07；CT/test_raw_output_export_creates_immutable_artifact | 原始快照/区间与 SHA-256 |
| E08 | PASS | L/E08 | 数量上限、模拟低磁盘拒绝、自写失败；accounting_only 非硬 quota |
| F01 | PASS | CT/test_wait_sets_retry_wakeup_after_run_enters_waiting、test_execution_finish_gate.py | 两有限 job 和保留 listener，any/all 与延迟进入 waiting |
| F02 | PASS | L/F02；EL/test_execution_ledger.py；CT/test_execution_delegations.py | 无活动 run 仍入库，服务重启序号稳定 |
| F03 | PASS | CT/test_event_receipts_are_idempotent_and_cursor_moves_once | 事务提交前后重取、收据幂等及游标 |
| F04 | PASS | CT/test_execution_finish_gate.py、test_execution_delivery_recovery.py；UI | 无委托不合成模型唤醒；使用确定性调用路径，未调用真实模型 |
| F05 | PASS | CT/test_once_ends_after_handling_run_while_continuous_remains | once 完成和 continuous 保留；checkpoint 确认另见 F09 |
| F06 | PASS | CT/test_cancelling_duty_releases_events_and_supersedes_queued_wakeup、test_cancelled_duty_blocks_old_model_response_and_syncs_revocation | 排队/旧响应/派发撤销窗口，持久化 revision 重试同步；非全部时序排列 |
| F07 | PASS | CT/test_paused_or_recovering_does_not_strand_delivery | 暂停/恢复只保留事件，不自行解暂停 |
| F08 | PASS | CT/test_execution_delegations.py、test_execution_summary.py | UI 阅读与模型交付分离，摘要有界 |
| F09 | PASS | CT/test_delivery_batch_crash_windows_are_idempotent、test_consumed_notification_is_replayed_until_graph_receipt | 实际 AsyncPostgresSaver checkpoint 与稳定 message_id；未持久确认前重放 |
| F10 | PASS | CT/test_duty_and_user_message_race_create_one_followup_run | completed 旧 run 不复活；只创建一个新活动 run |
| F11 | PASS | CT/test_execution_event_cannot_become_user_authorization、test_execution_delegations_unit.py | 事件按非授权数据注入，拒绝工具输出充当用户委托 |
| F12 | PASS | EL/test_watch_rules.py | 跨块、冷却/去重、有界正则与超时禁用规则 |
| G01 | PASS | CT/test_execution_finish_gate.py、test_execution_summary.py；UI | 待办/资源/值班三者区分 |
| G02 | PASS | CT/test_summary_marks_availability_notes；UI 与代码复核 | 只提示无输出、无空闲清理；未做长达数日的挂起试验 |
| G03 | PASS | CT/test_execution_api.py、test_execution_final_controls.py；UI | 取消值班、关闭、暂停、终止各自边界 |
| G04 | PASS | L/G04；CT/test_execution_delivery_recovery.py | 旧 fence/委托拒绝，撤销未同步时 pending |
| G05 | PASS | CT/test_delete_keeps_unknown_resources_visible、test_terminate_all_and_delete_precheck | 有不确定资源时拒绝破坏唯一可管理记录 |
| G06 | PASS | 完整 CT、完整 EL | HTTP、浏览器相关确定性测试、文件版本/证据/模型设置/暂停恢复；真实浏览器目标和模型效果未跑 |
| H01 | PASS | deployment.json；更新前活动核对、备份和独立 fixture 清理 | 凭据/数据/暂停状态保留；未启动用户排队 Runner |
| H02 | PASS | local-checks.json；deployment.json | 本机 frozen 依赖/生成/构建及独立安装包 |
| H03 | PASS | deployed-readonly/results.json、deployment.json、final-remote-verification.json | 实际进程 boot、安装路径及 21 个源码哈希 |
| H04 | PASS | desktop-launcher.json、desktop-execution-panel.png、final-launcher.txt | 原快捷入口链路、API-only 启动及新静态资源；不启动真实模型 |
| H05 | PASS | deployed-smoke/results.json | 专属无害 fixture 的命令、日志、PTY、取消/事件 |
| H06 | PASS | rollback/results.json、rollback/legacy-source-manifest.json；增量迁移、2857 文件备份核对和隔离还原 | 隔离 fixture 的活跃非交互资源经历旧二进制/新版恢复；身份及单次启动保持、日志连续、取消确认。旧版不接管新协议，未降级日常服务或活跃 PTY |

64 个 ID 均有上述范围内的检查证据；不能据此概括为所有场景及故障时序排列均已验证。真实模型/目标不属于本表通过范围。

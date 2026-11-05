# 0019：Ultra / A2A / Pi 第一阶段

日期：2026-11-03。依据本次用户授权，扩展 0018 的单 Agent 产品边界；0015 的自然结束、0016 的图权威、0017 的聊天隔离继续有效。

- `ExecutionMode` 真源为 `contracts/delegation.py`，取值 `standard | ultra`。Session 创建时固定，Run 从数据库复制模式快照。历史记录回填 Standard；不存在运行中切换接口。
- Standard 保留官方 Python `create_agent`，不连接或启动 Node。Ultra 在同一工厂增加四个委派工具，主 Agent 可自行决定不委派。Worker 不可用明确报基础设施故障，不降级。
- Worker 为独立 Node 服务，使用官方 `@a2a-js/sdk` 1.3.0（A2A 协议 1.0）及 `@earendil-works/pi-agent-core` 1.5.0。Python 使用 `a2a-sdk` 1.3.0；仅 JSON-RPC HTTP polling，不启用 0.3 兼容、流式或推送。
- 第一阶段只提供 Mock Provider / `mock_observe`。Pi 自带 Agent Loop 负责多轮模型与工具执行；没有 Shell、网络目标访问、Executor、模型供应商凭据、孙 Agent 或固定漏洞角色。
- 本地 `app.agent_delegations` 管委派意图；`ultra_worker.requests/tasks` 分别管远端去重和 A2A Task。它们不是第二份 Checkpoint、Plan 或 Executor 执行台账。
- 委派 ID 为父 Run UUID 与 ToolCall ID 的 UUIDv5；先提交本地意图，再发 A2A。远端生成独立 Task ID / Worker ID，以稳定 message/request ID 做持久去重。响应丢失只查询，不盲目重发。
- Worker 接受意图和初始 Task 原子落库；`claim` 只允许 `reserved → running` 一次。结果、终态与完成阶段在同一远端事务提交，终态不被迟到事件覆盖。
- 父暂停/取消在控制事务中持久化子任务取消意图；独立后台轮询执行取消，不阻塞 Standard 调度。取消墓碑阻止晚到提交。父暂停后不恢复原 Pi 循环，必须显式创建新委派。
- Worker 重启后保留未完成 Task 并标记 `TASK_STATE_UNSPECIFIED` / `worker_restart_no_pi_resume`，不自动重建 Agent。没有无损 Pi 断点续跑承诺。
- Worker 结果经身份映射、结构和大小验证后进入 CAS，主 Agent 只接收有界摘要与工件引用。模型描述和 Mock 工具观察不产生 Finding / Verification / confirmed。
- Worker 固定回环监听、服务令牌鉴权。模型不能选择服务地址。独立数据库角色仅允许访问 `ultra_worker` 表；Windows 服务凭据用当前用户 DPAPI 加密，位于既有 secret 目录旁，不写入源码、工件或数据库备份。
- SDK 1.3.0 的 Express Agent Card handler 直接 JSON.stringify 内部 oneof；本项目用官方 `AgentCard.toJSON` 显式序列化 Card。JSON-RPC 客户端和服务端仍使用官方实现。

实测、部署范围及恢复限制见 [交付报告](../../reports/ultra-stage1/implementation-report.md)。真实 Provider、真实 Executor 工具适配和目标效果不属于本阶段验收。


## 2026-11-03 审查补修

六项 P1 及摘要回流已按 [补修报告](../../reports/ultra-review-fixes/repair-report.md) 修正。
SDK 只保存非终态投影；执行方退出后才能原子提交终态。取消在 claim 前已有可等待句柄，
无句柄的 running/interrupted 不作为已停止。恢复事务使旧 complete/save 失效；失锁停止接单并中止活动循环。
主 Executor 与 Worker 的取消独立发起；取消队列和委派轮询按尝试时间推进，停止意图优先。
已核实的执行终态与结果校验分开：坏 Artifact 保留结束状态、记录拒绝原因，且不写结果工件。

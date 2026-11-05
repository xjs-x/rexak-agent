# 0021：Ultra Goal 第二阶段按需证据读取

日期：2026-11-04。继承 0019 的 A2A/取消补修和 0020 的 Provider、交接与用量机制。

- 继续原 Main create_agent 和 Pi Agent Loop。Worker 增加 list_evidence / read_evidence，通过固定回环控制端通道读取；不接真实 Executor、不读业务 PostgreSQL 或任意本机文件、不加入 Planner。
- 新委派的 WorkerTaskPackage.evidence_access 为 session_read_v1；只由 Python 写入持久 handoff_snapshot。原输入摘要不变，历史快照不回填权限，重放不升级旧任务。
- context_refs 是推荐引用，不是 Session 全部可读工件清单。控制端校验 Session owner、Run/委派关系、远端持久 Task/Worker/context 身份、running、取消意图、租约、owner generation、control version 和授权快照 tool_allowlist。
- 复用既有服务密钥，在运行时派生绑定 delegation_id/task_id/worker_id/context_id 的 HMAC 凭据。凭据不进入 A2A 包或模型工具参数；服务 URL 固定在后台。共享 Node Runtime 属于可信计算边界，不宣称对已被攻陷的进程提供隔离。
- 每次请求核对远端 running 身份，随后按 Session → Run → Delegation 顺序加锁，读取与控制意图在控制端排序；读取结束再次检查租约。没有取消/暂停后的缓存通行证。
- 允许同 Session 历史工件，排除 secret、ModelCall response archives 和内部证据交付收据。来源 URL/文件键不交给 Worker。二进制、非法 UTF-8 和超过 8 MiB 的源工件明确拒绝，不猜编码或隐式提取。
- 复用 ArtifactStore 的根路径校验、受限读和现有脱敏规则；校验实际读入原件的大小和 SHA256，完整脱敏后按视图 UTF-8 字节范围返回，最多 16 KiB。视图哈希与原件哈希分开；next_offset 与截断语义明确，不对原始字节片段事后脱敏。
- 只新增 app.delegation_evidence_reads。稳定收据 ID 由 delegation_id + Pi ToolCall ID 派生；请求摘要冲突拒绝，同请求返回 CAS 中原响应。元数据、返回片段、范围、来源、失败信息落现有 Artifact/CAS，不另存完整 Agent 消息历史。
- 收据证明控制端读取和形成响应，不能证明模型理解，响应丢失时也不能证明 Pi 收到。Worker 只有收到成功非空读取响应后才记录 evidence_reads；目录/初始推荐引用不算正文读取。
- WorkerResult.evidence_refs 可非空，但必须与 evidence_reads 对应。Main 逐一核对本委派收据、CAS 响应、读取范围、视图哈希、当前来源元数据及实际原件哈希；不合法结果拒绝写工件，执行终态独立保留。合法引用也不创建 confirmed Finding。
- 本阶段不实现实时推送、追加消息、Pi 历史续用、递归委派、任务 DAG、共享黑板或跨 Session 记忆。Standard 的工具、读取语义和授权路径保持。

实施、实际回归和边界限制见 [交付报告](../../reports/ultra-goal-stage2/implementation-report.md)。

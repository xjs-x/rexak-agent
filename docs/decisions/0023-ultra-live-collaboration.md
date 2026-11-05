# 0023：Ultra Stage 4 有界协作事件

日期：2026-11-04。继承 0019–0022 与 Stage 3 修复；本决策不实现 Stage 5。

- 仅新委派增加 collaboration_access=collaboration_v1 和 report_finding 授权；原授权快照不回填。新执行 Worker 有六个基础工具、两个证据工具和 report_finding。Standard 不装配协作 Middleware、不调用 Worker。
- Worker 发现与 Main 补充消息共用 app.delegation_collaboration，只保存有界协作数据和交付回执，不保存完整私有历史、任务图或第二份 Agent 状态。发现 UUID5 由 delegation_id + finding:ToolCall ID 派生；Main 消息 UUID5 由 parent_run_id + delegated-message:ToolCall ID 派生，请求摘要包含目标 delegation_id，防止同一父调用改目标而投递两次。同键异参拒绝。Session→Run→Delegation 锁内分配 Run 序号并提交，避免并发先分配后提交导致游标漏读。
- report_finding 复用固定回环控制端、服务令牌派生的任务 HMAC、A2A 持久身份和原父控制守卫。运行时按模型选择的 Artifact ID 从实际 evidence_reads/output_refs 填充来源，Python 校验收据、归属、范围、SHA/CAS。无引用候选允许存在；所有发现均 unverified_candidate，绝不创建 confirmed Finding。
- Main 使用官方 create_agent 的 abefore_model 节点，位于压缩之后；有序读取当前 Run 的未交付发现，稳定 HumanMessage ID 和 collaboration_cursor 在同一图更新中提交。没有提前写业务 consumed 标志；Checkpoint 失败会重读同 ID，模型失败后保留原消息。游标属于 Checkpoint，事件属于业务表。
- wait_delegated_task 用 Checkpoint 游标轮询当前 Run 新发现，有发现便返回 ID；随后 Middleware 注入正文。不会中断当前模型请求或 Executor 操作，不自动恢复任何暂停/终止 Run。
- 当前官方 A2A SendMessage 在本服务承担 reserve/execute 新任务。现有 DefaultRequestHandler 并不提供把消息精确交给某个存活 Pi 实例的原子接口，因此使用已认证 /requests/{request}/messages/{id} 受控扩展；保留官方 A2A 创建、查询、取消、结果协议，不增加私有 JSON-RPC 方法。消息始终绑定真实 A2A task/worker/context。
- Main 先持久化 accepted，再经每 Worker 有序发件、Node durable inbox 去重，标记 queued。Node 在原 Pi Agent 上调用 steer；原生 message_end 的可等待监听器再次检查父授权，成功纳入上下文后才记 consumed。queued 与 consumed 分开，后者不证明模型遵从。模型/工具运行不中断；内容是 user 数据，不能改变 frozen tool loadout 或授权。
- Node Inbox 在入队前持久登记；不确定窗口不自动二次注入。崩溃/上下文消失后未消费记录标记 context_lost；不重建 Agent 或宣称无损续跑。控制端响应丢失优先 GET 核对，缺失时同 ID 重投由 Inbox 去重。终态只允许读取旧回执，禁止新投递。
- 本阶段不实现 Worker 间直接通信、自动广播、实时全量输出 UI、消息历史恢复、跨任务续用、递归委派、Flag 验证。现有 Scope、Executor fence、Operation 幂等和未知状态语义保持不变。

官方 Middleware 语义参考：[LangChain before_model](https://reference.langchain.com/python/langchain/agents/middleware/types/before_model)；Pi 行为以当前锁定 1.5.0 已安装 agent.js / agent-loop.js 为准，测试验证原生 steer、等待监听器及真实 Provider 请求上下文，未套用不同版本示例。

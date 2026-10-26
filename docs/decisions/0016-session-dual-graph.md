# 0016：Session 双图与轻量周期同步

日期：2026-10-23。执行用户确认的第三阶段方案，取代旧领域工作记忆默认路径。

官方 create_agent 仍是唯一主循环。Session 信息图/攻击图为领域记忆，唯一权威为 app.session_graphs；LangGraph 执行图状态、消息、PlanStep/CurrentFocus 继续归 checkpoint。app.session_graph_receipts 保留稳定调用回执和修改前后快照，不是第二份可写记忆。迁移 b903eaf18201 只新增两张表，不转换或删除历史领域记录。

模型通过 get_graph / update_graph 维护 Entity、Relation、AttackState、Attempt；程序检查 Schema、同 Session 来源、引用、版本、控制与租约，不能将语义判断包装成漏洞验证。允许失败、自环、多来源、空更新。Session 行锁后再锁 Run，与已有运行控制写入顺序一致；图、修订回执和已呈现事件页游标同事务。复用 Run+ToolCall 身份，不随机创建重放对象。

完整结算的主模型工具决策按 AIMessage 稳定 ID 去重计数，图工具专用轮次不计。默认每五轮在下一请求的 WorkingContext 呈现完整图及一页待核对执行事件。操作/Job 持久投影指纹补写稳定 Session 事件，避免依赖时间戳/UUID排序。每页最多200个事件，明确剩余事件；只确认实际呈现页，未呈现资料不推进游标。忽略提示后每个周期才提醒，不拦工具、暂停、取消或自然结束。不新增模型、Planner、反思节点或 GraphGate。

盲测默认关闭项目经验与未分类方法检索；旧 Observation/Lead/Finding 表和显式历史查询保持。关闭 session_graph_enabled 可回到旧目录与工具路径，blind_test 仍默认隔离跨任务经验。graph_attack_enabled、graph_inject_every_round、graph_sync_interval 用于对照实验。

图写入与读取复用脱敏；只存短语义和来源 ID，图中内容是数据，不能扩 Scope。完整图超过动态预算时明确标明未呈现，清除确认凭据，允许 get_graph 召回；原图不裁剪。已有 ContextBuilder 最终预算保护保留，GuardedSummarization 不写双图。

验证范围和成本见 reports/session-dual-graph-implementation-2026-10-23.md；确定性测试不能证明真实模型的选择与语义维护质量。

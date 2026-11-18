# 0027：Session 双图共享感知与渐进检索（P1）

日期：2026-11-18。继承 0016、0021、0024、0026。仅扩展共享读取，不增加 Worker 写图、Planner、图数据库或模型调用链。

- 权威仍为 `app.session_graphs.document` 与独立 revision。`domain.graph_read.read_shared` 只调用既有 `read_graph`，不扫描 Operation/Job，不触发 GraphReview，不创建图回执或推进同步游标。不增加可写缓存。
- Main 与 Pi 共用 `GraphRead` / `GraphQuery` 契约和控制端实现。Pi 参数 Schema 与固定共享提示由 Python 生成；任务身份、Session、HMAC 与控制端 URL 不属于模型参数。
- `graph_overview` 按存储的 Relation.status / Attempt.outcome 分类，完整计数并按持久化 ID 稳定选取有界样本，补齐真实端点及 entity_refs。它是部分全局索引，没有重要性评分、语义总结或事实可信度提升。未展示对象可从完整图独立搜索、按 ID 查询或分页读取。
- `graph_query` 支持图类型、对象类型、多个 ID、字面关键词、状态/结果、revision 与分页。按真实边和 entity_refs 展开，默认一跳、最多四跳，可继续查询下一层；边保留独立持久化 ID，自环、反向边、平行边、环路和重复路径均去重到对象 ID，不合并不同边。
- 展开时 graph 限定种子所在图，cross_graph 允许返回另一图；返回种子、关系和端点，kind 不裁掉邻居。status/outcome 约束对应图内的遍历边，跨图只使用实际 entity_refs。缺失引用不补造节点。
- 查询默认 40、最多 200 个对象；最多 64 个种子，每 ID 最多 128 字符；查询响应预算为 256–8192 估算 Token。整对象装入，不能容纳首对象时返回明确错误，不截断为伪完整记录。分页游标绑定 Session、查询条件、功能开关和 revision；版本变化明确拒绝，必须重新查询。没有历史快照拼接。
- 概览每类默认最多 4、最大 20 条边；预算不足逐项移除样本及其端点，再不足则返回 `omitted`。极低预算仍保留最小协议元数据和查询入口，省略的是图正文；它不能证明历史页已呈现。
- 自适应读取按完整序列化内容估算，返回 `full` / `overview` / `omitted`。Main 在 ContextBuilder 的完整实际请求预算内分配剩余空间；Pi 图任务在自身 context_window 扣除实际 max_tokens 后保留 10% 序列化余量，按完整发送视图 UTF-8 字节 / 3 估算（包含 Pi System 中的工具声明），并预留嵌套 JSON 转义成本；不再重复扣除旧工具预留。未启用图的旧任务保留原保守字节检查。首次动态交接不增加模型轮次，此后模型自主读图。
- 固定系统提示仅增加用户确认的共享说明（预算不足说明是兼容调整），动态图不进入 System Message。Main 保留不可变 host_context_update 与已有归档机制；超预算时旧的大型图投影也可经同一来源归档通道缩减。Pi 初始图只在首次 transformContext 进入发送视图，不写入可续用历史。初始读取失败明确省略，正常任务工具仍可用；取消继续优先。
- `graph_context`、get_graph(include_pending=true)、update_graph、page_id、expected_revision、已呈现页检查与提交回执保持原维护协议。只读图工具轮次不累计工作轮次。完整维护页放不下时不授予确认凭据；概览不能替代历史核对。
- 新委派在持久快照中显式获得 `session_graph_read_v1` 及三个只读工具。旧委派重放不升级权限；完成历史续用仍生成新身份并绑定原 Session。控制端复用现有远端身份、Session owner、Run 租约、generation/control_version、撤销、取消与生命周期守卫，按 Session→Run→Delegation 加锁后读取，结束再检查租约。无 Worker 写图接口。
- 图只返回真实 source_refs，不携带证据正文。Worker 的 list_evidence 增加 source_operation_id 过滤，将 Operation 引用解析为同 Session、既有可见策略允许的 Artifact 目录，再用 read_evidence 读取。目录为空就保留不可用状态，不声称支持任意 Operation 正文直读。Main 继续既有 search_history/read_artifact。引用有效不证明图中技术判断正确。

隔离数据库、官方 Main/Pi、假供应商和无害合成图验证及限制见 [P1 交付记录](../../reports/shared-graph-p1/implementation-report.md)。生产入口和服务未部署，真实模型效果未验证。

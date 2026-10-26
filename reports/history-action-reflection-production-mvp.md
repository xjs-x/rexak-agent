# 历史记忆与重复行动提示：生产 MVP

日期：2026-10-25。**已接入正式 Agent 生产代码，可供用户在授权测试环境中试用；默认 `off`。** 验收使用隔离 PostgreSQL、正式 API/Runner/Checkpoint 和确定性模型，未请求真实 Provider、Kali、Executor 或靶场。没有真实模型收益结论，也没有开展新的科研对照实验。

## 已接入的功能

新 Run 在创建时保存 `history_assistance_mode`，Runner 恢复其模式后，正式 factory 按需装配：

`ResponseRecovery → ContextBuilder → HistoryAssistance → ModelObserver → 原模型调用`

- `off`：不装配新增 middleware，新 Run 不启用 Shadow，保持原工具/模型行为。已有双图等原功能保持原配置。
- `memory`：复用 `build_blackboard/read_snapshot/select_memory`，最多呈现 3 条相关执行记录及 1 条模型声明来源导航。保留完整 Operation/Job/Artifact 来源引用、工件可用性/哈希、未知状态、适用条件与差异，明确“元数据不是正文已读或漏洞结论”。不复制 Artifact 正文或模型声明自由文本。
- `passive`：包含 memory；读取已有 Shadow 的 `raw_visible_v1` 高置信度触发，在下一次有机会的正常模型请求附一次中性参考。原 Agent 可以继续、调整或自然结束；没有第二个 LLM、额外反思请求或工具拦截。

参考只进入**发送视图**：优先在原配对 ToolMessage 的 JSON 上附 `_host_history_reference`；初始请求没有工具消息而确有相关历史时，用明确标注宿主参考的请求级 AssistantMessage。两者都不是新用户命令或 SystemMessage。原消息对象、checkpoint、ToolCall、工具回执及业务结果不变。参考在 ContextBuilder 归档逻辑之后添加，再做最终输入预算检查，避免被归档成原始工具事实。

## 启用、关闭与回退

在控制端 `.env` 设置并重启控制端 API/Runner，随后新建阶段：

```dotenv
WORKBENCH_HISTORY_ASSISTANCE_MODE=memory
```

可选值为 `off / memory / passive`，默认 `off`。模式固定保存于新 Run，单纯修改默认值不会热切换正在执行的 Run。关闭未来运行时设为 `off`。

已有 Run 需要立即回退时，先按原流程暂停，再设置并重启控制端，恢复原 Run：

```dotenv
WORKBENCH_HISTORY_ASSISTANCE_FORCE_OFF=true
```

此运维开关覆盖已有模式快照，停用新增辅助与 Shadow，不删除历史、不改 checkpoint 或操作身份。重新允许辅助时移除此覆盖或设为 false；原 Run 的请求模式仍可追溯。没有直接编辑生产数据库的要求。

仅开启 `passive` **不会注册额外工具或授予执行权限**。重复提示目前只支持已有固定 HTTP 安全测试配方，须由操作者明确配置：

```dotenv
WORKBENCH_HISTORY_ASSISTANCE_MODE=passive
WORKBENCH_HISTORY_ACTION_TEST_PROFILES_ENABLED=true
WORKBENCH_HISTORY_ACTION_SHADOW_CONTEXT_PATH=C:/path/to/controller-task-policy.json
```

授权文件沿用现有 `TaskAuthorization` 合同，必须匹配当前 Session/Run、控制/Scope/策略版本、执行端身份/boot、有效期和固定请求。**不能用随意填写的环境/配置字符串冒充实际条件。** 只有显式开关、配置路径与 HTTP 能力预检均满足，生产 registry 才注册 `run_http_test_profile`；执行时仍核验有效授权、原 Scope、控制版本和全部原 HTTP 守卫。未知配方、失效授权及能力关闭均不能通过此入口执行。

普通 HTTP 导航、动态认证、无法确认测试语义的请求只使用 memory，不创建 Shadow 观察服务。旧逐调用批准路径仍可用于原研究 Shadow，但本 MVP 不把它自动升级为生产被动提示。

## 可靠性与边界

- 默认检索期限 0.5 秒，最多 3 条执行记录；memory 投影最多 8 KiB，合并参考最多 16 KiB，再受真实模型请求预算限制。放不下则整条省略，不删条件来保留结论。
- 沿用队列 8、历史 64、输入 256 KiB、单记录 64 KiB、512 文件/8 MiB 等原 Shadow 边界，没有优化或放宽检测算法。
- 生产 passive 的 Shadow 只使用成功普通模型请求的实际可见消息投影；ContextBuilder 已归档/裁去的正文不能借 checkpoint 重新冒充已读证据，隐藏推理不进入该投影。
- 不等待 Shadow drain。未及时完成的观察可以延后；Agent 已自然结束则可能没有投递机会，不额外唤醒模型。
- 投递前重新核对当前授权、Run/Session/thread、控制/Scope/策略版本、Operation/Job 状态及原工件可用性/哈希。授权或条件变化导致旧提示失效时直接省略。
- 收据复用原 ShadowStore 的排他写入和总配额，同一 Run/thread/material 只 claim 一次，跨进程有效。收据先于模型调用；故障可能丢一次参考，不承诺 exactly-once，也不自动补请求。
- 历史检索、观察、存储、超时、过载和可选视图错误记录后继续原模型/工具路径。暂停、取消、租约失效和过期控制信号继续向原 Runner 传播；模型 handler 在可选处理的异常捕获之外，不会因失败再调用一次。
- manifest 记录模式、参考角色/位置/哈希、最终输入估算与省略原因；实际到达由模型请求输入验证，不把 enqueue/claim 认作模型理解。

## 验收结果

原始输出位于 `reports/history-action-reflection-production-mvp-evidence/`；`baseline.json` 保存任务起点的 4804 个文件哈希和脏工作区状态，`delivery.json` 记录最终范围及保全：9 个既有文件属于获准修改范围，4795/4804 个既有文件原字节不变；4200 个已纳入起点哈希清单的旧实验、报告和执行端文件全部原字节不变。另有 96 个前序开发预演归档长路径在起点只记录了文件名，未取得内容哈希；本轮只读补存当前哈希，不冒称完成起点字节对照。

| 验收 | 结果 |
|---|---|
| 新增单元与正式 Runner/端到端测试 | 38 项通过（含严格 UTF-8 外部字节测试）；包含三模式、原配方 registry、实际提示、配置变化/明确复测、忽略提示继续、检索失败、过载、暂停/重建/恢复、强制关闭回退、跨 Session 与条件限制 |
| `off` 与旧 Runner 比较 | 完整模型输入、派发、Operation/Job/Artifact 及 checkpoint 业务投影等价 |
| 相关 Decision Memory、Shadow、Runner、GraphReview、上下文预算等 | 109 passed，14 项既有测试序列化 warnings |
| 新生产源码/新测试 Ruff、默认 Pyright、UTF-8 | 通过 |
| 冻结依赖本机离线同步、前端 typecheck/build | 通过 |
| 全树 Ruff | 7 项既有研究文件问题，未修改旧冻结镜像制造全绿 |

端到端断言使用实际模型收到的消息：三次工具执行对应四次正常模型请求；passive 在第三次正常请求收到一次 ToolMessage 参考，Agent 仍执行后续工具。原 checkpoint 和 Operation 结果中没有 `_host_history_reference`。初始历史场景还验证 unknown 保留、身份修订 1→2 的 changed 条件、原 Artifact 引用及跨 Session 拒绝。

已知 `loaded_code_version` 指纹问题和上游取消异常问题未扩大修复范围；本功能不吞控制信号，不宣称已解决既有问题。早期测试的假凭据缺失和测试类型问题保留日志；未通过改变真实执行授权来修测试。

## 试用范围与部署状态

**代码层面已达到授权环境试用条件。** 不依赖科研盲评或 24 Run Pilot 放行。真实模型是否正确利用参考、是否减少无价值测试、是否改善净收益仍未验证；当前没有这种效果承诺。

主要限制：跨 Run 历史仍受同 Session、现有来源/授权及结构关联约束，不做跨任务全局检索；仅近期但与当前 Run/任务无可信关联的历史不强塞。动态认证和普通导航不支持重复提示。缺少合法固定配方发布时，passive 可正常提供 memory，但不会产生重复提示。

生产 System Prompt、Decision Memory v1、检测算法、执行端源码以及旧实验 Oracle/冻结/账本未修改。没有 Git 提交/推送，没有数据库卷清理或生产业务库修改。

本机依赖和前端构建通过，原桌面 `Rexak Agent.lnk` 仍指向本仓库 `start-workbench.cmd`；本轮没有启动该入口或重启现有服务，也没有连接 Kali 核对安装进程。**因此交付的是已验证的生产代码，不声称正在运行的桌面/Kali 实例已经加载新版。** 这是遵守本轮不得自动调用真实执行环境的限制。

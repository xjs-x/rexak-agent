# 第二轮：移除 FinishGate 与执行可靠性实现报告

日期：2026-10-22（北京时间）。基线提交 `bc84790`。本轮代码与测试保留在工作区，没有自动提交、合并或推送。复盘输入为 `reports/benchmark-review-2026-10-22/` 的 review、metrics 与 evidence；历史攻击结论只作已提供证据，不在本轮重新执行。

## 1. 实际改动

| 入口 | 实际行为与边界 |
| --- | --- |
| `agent/factory.py` | 新图不装配 FinishGate。仍只调用官方 `create_agent`，没有新建决策主循环。仅检测到历史 FinishGate 节点时装配无副作用兼容桥。 |
| `agent/middleware/finish_gate.py` | 删除提议审查、跳转反馈、模型重试、完成文本生成和 review span/event 写入；两个历史 hook 只返回 None，沿官方图继续。 |
| `tools/completion.py` | 删除整个专用结束工具文件与新写入路径。Registry 不再导入或注册结束工具；旧 ToolSelection.completion 配置仅兼容接收，即使 True 也不会恢复工具，manifest 固定 False。 |
| `runtime/runner.py` | 删除第二次 FinishGate 审查、业务拒绝恢复、提议事件投影。识别官方无 pending 节点的自然 END，最后消息必须为无 ToolCall/invalid ToolCall 的非空 AIMessage。中间分析文本、ToolMessage、分段 yield 不视为完成。 |
| Runner 最终归档 | 从已提交 checkpoint 归档真实正文为不可变工件；稳定 ID 去重，配置只存读取指针与模型调用关联。归档通知不再次追加流式正文。API 对话用关联去重，不伪造模型调用；原 ModelCall、Token 和响应工件保留。 |
| Runner 运行收尾 | 使用已有所有权与控制检查；最终状态事务在 Session 锁下刷新并锁定 Run，再检查租约/代次、暂停、取消与固定控制版本。迟到 owner 不能修改新 owner 的状态；过期版本保留 checkpoint 进入恢复；提交瞬间新到的取消保留 cancelling，交原取消核对链确认资源停止，不能直接虚报 cancelled。 |
| `contracts/enums.py` | 新增 `model_finished` 作为自然结束原因，保留旧原因兼容读取。completed 不表示安全目标达成或漏洞已经确认。 |
| `agent/state.py` / `state_migration.py` | 状态版本 8；CompletionProposal、finish_gate_feedback/attempts 移入 LegacyProtocolState，新状态不初始化、不消费也不更新。v7→v8 保留历史提议、消息和 pending 边界，未提交并行结果仍拒绝迁移。 |
| 系统提示 / 方法资料 | 提示版本 13、方法资料版本 9。不再提结束工具、完成证明或结束引用；明确遵守用户停止条件并复用已有结果生成自然报告。 |
| `domain/reports.py` / API | 按需生成的现有报告可包含最终回复原文工件；先核对同会话归属与哈希，再脱敏展示，标明是模型原文，不提升 Verification。前端整体布局没有改写，OpenAPI 类型重新生成。 |
| `tools/jobs.py` 日志导出 | 从同会话 Job/资源关联确定原始 Operation；唯一可靠来源才自动关联，缺失或歧义保持 NULL。不使用导出动作冒充原执行，不回填历史工件。 |
| `get_job` / 后台唤醒 | 查询返回身份与当前 Session/原 Operation 匹配后，同步仍未结算的 Operation 和 Job。既有 failed 业务结果不被 Job succeeded 擅自覆盖，原工件列表保留。 |
| `wait_for_resources` | 明确只接受 execution/v1 命令、脚本、终端、监视器资源。HTTP/浏览器/文件属于旧 Job 接口，应使用 get_job；错误返回给出正确接口，不扩展等待协议。 |
| Executor HTTP / 控制端恢复 | HTTPError 后台账使用 unknown，并保留错误码；旧 failed HTTP 传输回执经统一适配为 unknown/uncertain，用于 HTTP 工具、get_job、后台唤醒和恢复核对。构造失败仍为 not_applied，取得真实响应才为 delivered。 |

`runtime/stopping.py` 的旧 FinishGate/CompletionDecision 留作离线兼容服务和历史测试，没有生产调用者；runtime 包不再重新导出它们。历史数据库表、旧计划与领域审查模块没有物理删除。旧节点桥不等同于保留业务 Gate。

Runner 不读取 PlanStep、Lead、Coverage、Finding、Verification 或最终文本中的工件引用来决定是否结束。最终答案可以说明“条件不足”“没有完成目标”“未发现漏洞”；不存在的文本引用不会生成 Artifact，也不会触发模型补证据循环。

## 2. 新的最小执行链路

1. Web 继续使用现有 Session/消息 API：`api/routes.py` 创建 Run、写入或排队用户指令。
2. `AgentRunner.step` 领取租约，`_run_segments` 装配能力、工具箱、运行上下文与版本；不增加业务完成判定器。
3. `agent/factory.py:open_agent_runtime` 装配官方 `create_agent`、PostgreSQL Saver/Store 和原有上下文、观测、控制、分段与 ToolDispatch middleware。
4. 模型选择工具；ToolDispatch 校验能力、参数及固定控制版本。OperationRegistry 先持久化 `(run_id, tool_call_id)` 对应的操作身份，再走原 Executor 接口。
5. Executor 执行、Ledger 去重；原请求、响应、stdout/stderr、工件及哈希继续归档。ToolResult 经官方 ToolMessage 返回下一轮模型。
6. 模型自然最终回复使官方图到 END。Runner 区分 yield、pending 节点与自然最终消息；有新指令继续处理，有已派发未决工作则等待/恢复。
7. `_archive_final_response` 保存提交后的真实正文，`_finalize` 在控制状态仍有效时提交 completed / model_finished。已有 API、对话分页、正文读取和按需报告展示结果。

流程不需要 Plan、Observation、Lead、Finding 或 evidence_refs，也不为结束额外调用模型。存在未决执行时等待系统结算属于程序生命周期，模型不会收到“证据不足，继续攻击”的 Gate 意见。

## 3. 执行保护审计

| 能力 | 现状与证据层级 |
| --- | --- |
| 授权 / Scope / Capability | 原 ToolDispatch、HTTP/浏览器预检与 Executor NetworkPolicy 保留；自动化回归继续验证越界拒绝和工件隔离。 |
| 租约 / 控制版本 | 原 CommandGuard、Toolbox.assert_control_current、派发检查保留；新增自然结束与最终提交竞态测试，不以源代码存在代替测试。 |
| 操作身份 / 幂等 | 原 OperationRegistry、请求摘要冲突检查、Executor Ledger 保留；同一 ToolCall 恢复复用 Operation，新 ToolCall 可以创建新 Operation。已有恢复测试验证不自动重放未知操作。 |
| 分段与持久化 | 原 SegmentMiddleware、同步 checkpoint、控制交付回执保留；多轮自然结束跨分段、关闭 runtime 后读取 Saver、新 Runner 接续暂停均有受控测试。 |
| 取消 / 超时 | 保留独立处理，异常不会统一标 completed；取消用例验证后续工具不执行。未确认远端停止仍按 unknown 处理。 |
| 异步 Job | 原 accepted、状态查询、后台核对和唤醒机制保留。新测试验证 Job/Operation 一起收敛，未决 Job 不被最终回复丢弃。 |
| 执行未知 | unknown 不是目标失败；同一 Operation 的自动恢复只查询和核对，不无条件重做。未知结果阻止正常完成/自动唤醒，保留 waiting_tool 或 recovering。 |
| 来源真实性 | Operation/Job/Artifact 及 Session 校验、哈希、领域工具证据规则保留。新日志来源关联不虚构、不修改旧工件。 |

对模型主动选择的新 Operation，本轮不实施命令字符串去重或语义审批。保留模型自由并不意味着保证模型不会主动选择重复请求；这一效果需要用户的真实模型回归验证。

## 4. 自动化验证

本轮使用隔离随机 PostgreSQL 测试库、本地文件工具、脚本模型与测试执行器。未调用真实模型，也未请求 Apache、Struts2 或其他真实目标。

新增 / 替换的关键测试：

- `test_minimal_autonomous_loop.py`：零工具、单轮、多轮、可选未完成计划、并行 ToolCall、中间普通文本、自然最终正文与 API/报告、Token/模型调用次数、完整原始工件；新增暂停后换 Runner 恢复、取消后不再派发。模型调用数等于实际脚本决策数，没有额外完成审批轮次。
- `test_natural_completion_safety.py`：13 个展开用例，覆盖最终回复前暂停/取消/失效租约/过期控制版本，最终提交事务的竞态，running/unknown Job、无可核对 Job 的有限执行、提交后重建归档去重及非最终消息。
- `test_execution_result_reliability.py`：6 个展开用例，覆盖 browser succeeded 查询同步 Operation、旧 HTTP failed 传输错误保留 unknown、等待接口契约、日志确定/缺失/歧义来源及 HTTP 响应中断不自动重试。
- `test_state_v3_migration.py`：现有迁移测试延伸到 v8，另验证 v7 FinishGate pending 节点不审查、不改历史、自然回复只调用一次模型。
- 旧结束重试/耗时测试改为“没有审查反馈/没有审批耗时”；候选不自动确认、局部计划版本一致性和恢复不重放断言继续保留。

最终命令与结果如下；早期数据库停止导致的 fixture ConnectionTimeout 不计作通过，启动既有容器后重新运行，未清空数据卷。

| 检查 | 最终结果 |
| --- | --- |
| `uv sync --frozen --all-packages --group dev` | 91 个包核对通过。 |
| `uv run ruff check .` | 通过。 |
| `uv run pyright` | 0 errors / 0 warnings。 |
| `uv run python scripts/check_encoding.py` | 528 个代码文件通过 UTF-8 无 BOM、无替换字符/检测到的乱码。 |
| `uv run pytest tests/unit tests/integration tests/recovery -q` | **747 passed / 3 skipped / 10 warnings**，372.63 秒。最后一次完整运行通过；收尾竞态与恢复专项另有 63 passed（111.32 秒，与全量重叠，不重复累计）。 |
| OpenAPI 导出 / `pnpm generate:api` | 通过，生成类型包含 model_finished，保留旧结束原因。 |
| `pnpm install --frozen-lockfile` / `pnpm build` | 通过，TypeScript 检查及 Vite 177 模块构建。 |
| `git diff --check` | 通过。 |

完整收集清单见 `test-inventory.txt`，结构化结果见 `validation-results.json`。3 项跳过分别为两个 Windows 符号链接权限用例与未配置 Key 的真实 Xiaomi 探针。10 条警告来自既有 SimpleNamespace 测试替身的 Pydantic 序列化提示。

真实模型/API Key 与真实目标效果测试均为 **NOT_RUN**。工程用例不证明真实模型一定遵守“成功后立即停止”，只验证程序不再因内部来源或业务条件退回结束。

## 5. 本机与执行岛部署验收

原桌面 `Rexak Agent.lnk` 仍指向 `C:\xjs\rexak-agent\start-workbench.cmd`。使用原入口 `--api-only --no-browser --smoke-seconds 45` 检查，不启动真实模型任务。数据库、SSH、Executor 预检通过，`/health` 数据库健康；控制台 HTTP 200。API 加载提示版本 13，入口指纹 `loaded-minimal-loop:e9b1ea8708ce3b826862f6b0012d4a2088b140a7` 与独立新进程计算一致；该指纹不是全进程字节码证明。

最初数据库与 Kali 未启动，初次检查未运行成功；用户启动服务后重新核对。Kali 更新前 0 活动作业；本轮 Executor HTTP 有实际源码变化，因此构建 wheel、部署、重启并核对新实例。旧 boot `9058-813ad31cb41b`，新 boot `385746-db639a43a8e9`；23 个已安装 Python 文件哈希与本机一致，身份/协议 1 一致，command/script/PTY/screen/monitor/durable_events/detached_job_recovery 能力预检可用，pip check 通过，更新后仍 0 活动作业。未发起攻击验证。

验收前后业务库均为 **9 Session / 9 Run / 513 Artifact / 205 Operation**；全部历史 Run 的状态、更新时间、代次及控制版本不变，`.env` 哈希不变。启动冒烟仅停止本次创建的 API 子进程，保留数据库与 Kali。脱敏部署证据见 `deployment-evidence.json`。

## 6. 保留的问题与验收界限

- **已修复且自动化覆盖**：新运行不再有业务 FinishGate/结束工具；自然回复保存；运行控制收尾；日志来源可确定时自动关联；Job 查询同步 Operation；传输未知不会被当成可安全自动重试的目标失败。
- **现有机制与回归覆盖**：Scope、授权、能力、操作身份、并行消息、控制版本、租约、checkpoint、恢复、跨 Session 读取限制及原始工件真实性。没有重新编写 Executor 或 ToolDispatch。
- **仅作兼容保留**：旧 stopping 离线审查服务、历史 CompletionProposal/结束原因、数据库表和旧领域模块。旧 pending 结束工具本身不再执行；无法安全迁移的 pending write 明确拒绝，不猜测和自动重放。
- **没有在本轮真实故障实验验证**：目标端收到请求后断开响应的真实网络故障；运行中断电、跨主机进程崩溃；真实模型停止纪律与长期自主效率。本轮对此只有本地故障替身及现有恢复测试，不能标成真实验收。
- **明确的接口限制**：wait_for_resources 不支持旧 browser/HTTP/file Job；get_job 查询已验证可用，没有扩展第二套等待协议。浏览器超过交互阈值后，控制端工件收集仍沿既有读取机制，本轮没有重设计全量后台证据同步。
- **未知状态处理仍需人工核对**：无法确定执行结果时不会自动完成或自动重试；核对后沿现有恢复接口处理，不新增自动语义去重或人工审批框架。历史 failed/NULL 来源没有批量回写。
- **未来另行设计**：Verification、信息图、GraphRAG/RAG、历史记忆与摘要检索优化均未开展。已有 Observation/Asset/Lead/Finding 模型不重构，候选不会因 Run completed 自动 confirmed。

## 7. 用户后续最小真实回归

仅在现有本地 Apache、Struts2 两个授权靶场分别新建 Session，沿用原任务的“首次有效 RCE 和 id/whoami 身份证据后立即停止”要求。由用户运行真实模型：检查原始响应后直接自然报告、completed/model_finished、模型可见工具中没有 propose_completion、没有 completion.review，也没有为修复内部证据而新增攻击请求。

保留 ModelCall/ToolCall/Operation/Job/Artifact/checkpoint/Token 与首次 RCE 后调用数；Struts2 若再次响应中断，检查 unknown/uncertain 与原操作查询记录，不能只按 Job 或 HTTP 客户端错误称作目标未执行。不要为回归故意追加利用、提权或扩大测试集。本轮到此停止架构扩展。

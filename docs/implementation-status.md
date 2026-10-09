# 实现状态（P0–P9）

> 本文件是 `IMPLEMENTATION_CHECKLIST.md` 在本仓库的落地记录。
> **只有具备验证证据的条目才标 ✅**；未验证的一律标 ⬜ / ⛔，不用"已支持"文案。

- 记录日期：2026-10-08（P0–P2 第一版切片）
- 代码版本：见 `git log`（本仓库自身版本；`run.code_version` 记录的是运行时的代码标识）
- 控制端环境：Windows，CPython 3.12.14（uv 管理），PostgreSQL 16.4（Docker，端口 55432）
- 执行岛：Kali `192.168.72.134`，Python 3.13.12，Executor 装于
  `~/rexak-agent-executor`，数据根 `~/rexak-agent-data`
- 真实模型：小米 `mimo-v2.6-pro` @ `https://api.xiaomimimo.com/v1`

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ | 有代码**且**有可复现的验证证据 |
| 🟡 | 有代码，只做了部分验证（说明缺哪一项） |
| ⬜ | 未实现 |
| ⛔ | 明确关闭（有正当理由，且对外可见） |
| 🔵 | 由外部依赖决定，本轮 NOT_RUN |

---

## P0 · 建仓库、锁依赖、验证框架与模型兼容性

依赖：无。目标：排除"选型成立但版本不能配合"的风险。

| 任务 | 状态 | 证据 / 说明 |
|---|---|---|
| 创建独立 Git 仓库；确定包名、CLI、仓库名 | ✅ | 仓库 `rexak-agent`；包 `agent_workbench` / `workbench_executor`；CLI `workbench` / `workbench-executor`；**不 import `xak`** |
| 根项目 uv workspace，`executor/` 为成员，一个 `uv.lock` | ✅ | `pyproject.toml` `[tool.uv.workspace] members=["executor"]`；`uv.lock` 91 包 |
| 锁 Python / Node / pnpm 版本 | 🟡 | Python ✅（`.python-version`=3.12 → 3.12.14）；Node/pnpm 前端尚未建立（P7） |
| Python dev group、前端 pnpm-lock、Ruff/Pyright/ESLint/TS、Vitest | 🟡 | Python dev group + Ruff + Pyright ✅；前端工具链 ⬜（P7） |
| `.editorconfig`、UTF-8 无 BOM 检查、`.env.example`、`.gitignore` | ✅ | `scripts/check_encoding.py` 通过（`uv run python scripts/check_encoding.py` → 检查 90+ 文件；单测 `tests/unit/test_encoding.py`） |
| PostgreSQL compose 服务、持久卷、健康检查、最小 `db-init` | ✅ | `compose.yaml`；`uv run workbench db-init` 创建 3 schema + 框架表 + 19 张业务表 |
| `create_agent + AsyncPostgresSaver + AsyncPostgresStore` 最小兼容性测试 | ✅ | `tests/integration/test_framework_compatibility.py`（7 项） |
| 验证自定义 state、消息 reducer、中间件、工具调用、摘要、streaming、interrupt/resume、checkpoint 恢复 | ✅ | 同上。interrupt/resume 见"偏离说明"① |
| 假模型验证正常文本 / 多工具调用 / 工具错误 / 流分片 | ✅ | `tests/support/scripted_model.py`；工具错误行为见 decision 0001 §4.3 |
| 真实小米最小模型探针 | ✅ | `agent_workbench.models.probe` 实测 5/5 PASS（见 decision 0001 §3） |
| 确定统一的框架事件适配层 | ✅ | `contracts/events.py` 的 `EventEnvelope`；Runner 把图更新投影为事件，API/UI **不接触** LangGraph 原始事件字典 |
| 精确版本与结论写入 `docs/decisions/0001-stack-lock.md` | ✅ | 已写 |

**退出条件复核**：真实小米模型调用一个工具并收到结果 ✅；服务重启后同 thread 能读取之前状态 ✅（`test_checkpoint_is_readable_in_same_thread_across_compilations`）；Store 数据跨 thread 可按指定 namespace 查询 ✅。

### 偏离说明①：interrupt/resume

方案 P0 要求"用假模型验证 interrupt/resume"。实测结论：本项目**不使用** `interrupt()`
作为暂停机制。暂停/取消走 `runs.status` + `commands` 表，在模型调用与工具派发前的
一致边界生效（`GraphRunAborted`）。原因：`interrupt()` 恢复时会重跑中断点之前的代码，
无法保证非幂等副作用的唯一性（方案 §17 自己列出的风险）。

已实现并验证的替代语义：
- `run_state.ALLOWED_TRANSITIONS` 状态机（`tests/unit/test_pure_logic.py`）；
- 暂停/取消在一致边界生效、被拦下的 tool_call 仍形成合法终结结果
  （`tests/integration/test_tool_dispatch.py`）；
- 阶段结束后继续输入创建新 run、thread 不变（`tests/recovery/test_recovery.py`）。

---

## P1 · 权威数据、事件协议与最小 Executor

依赖：P0。目标：数据归属、身份和调用契约先稳定。

| 任务 | 状态 | 证据 / 说明 |
|---|---|---|
| contracts：Project / Session / Run / UserCommand / ToolOperation / ToolResult / Job / Artifact / EventEnvelope / ModelCallUsage | ✅ | `src/agent_workbench/contracts/`（enums / identity / tool / events / usage / errors） |
| 首批业务表 | ✅ | 19 张（含 `observations`、`spans`）；Alembic 迁移 `f11850acfff1` |
| Checkpointer/Store 隔离；框架表初始化与 Alembic 分开 | ✅ | 三个 schema：`app` / `agent_checkpoint` / `agent_memory`；`persistence/bootstrap.py` |
| run 状态转换服务与 session 单一活动 run 约束 | ✅ | `domain/run_state.py` + 部分唯一索引 `uq_runs_active_per_session`；测试 `test_only_one_active_run_per_session` |
| 幂等键校验：同键同内容返回原结果；同键不同内容 409 | ✅ | `test_same_idempotency_key_same_content_returns_original`、`..._different_content_conflicts`、并发 `test_concurrent_same_key_only_creates_one` |
| session 事件序号提交有序性、快照 cursor、事件分页补读 | ✅ | `observability/events.py`（`SELECT ... FOR UPDATE` 分配序号）；测试 `test_event_seq_is_contiguous_and_ordered`、`test_cursor_resume_does_not_skip_uncommitted_events` |
| 本地工件临时写入、哈希、原子落盘、引用读取 | ✅ | `evidence/artifacts.py`；测试 `test_artifact_write_is_content_addressed_and_atomic` 等 5 项 |
| Executor `/health`、`/capabilities`、一个真实 HTTP 工具、按 `operation_id` 去重 | ✅ | 岛上 `/health` 与 `/capabilities` 实测；去重测试 `test_reconnect_does_not_create_second_job` |
| 最小 Executor 也在目标执行环境运行 | ✅ | Executor 常驻在 Kali；控制端经 SSH 隧道访问，**没有**先在控制端跑通再迁移 |
| Executor 独立 SQLite 台账；控制端不直接访问其文件 | ✅ | `workbench_executor/ledger.py`；控制端只经 HTTP |
| SSH 隧道复用与最小 `ExecutorClient`；连接失败分类为基础设施故障 | ✅ | `execution/tunnel.py`、`execution/client.py`；`ExecutorConnectionError` vs `ExecutorRejected` 明确区分 |
| 最小范围策略、控制端本地访问认证、执行端管理接口鉴权 | ✅ | `domain/scope.py`（12 项单测）、`api/auth.py`（会话 Cookie + Origin 校验 + CSRF）、Executor Bearer 鉴权（令牌为默认值时**拒绝启动**） |

**P1 验证清单复核**

| 项 | 状态 | 证据 |
|---|---|---|
| 数据库迁移从空库成功；对已有数据升级不清库 | ✅ | 空库建 19 表成功；`db-init` 幂等 |
| 同一用户请求重复提交不产生第二个 run；并发也不双跑 | ✅ | 幂等 + 部分唯一索引测试 |
| 同一工具 operation 重试只产生一个远端操作 | ✅ | ledger 去重 + `test_replayed...`（控制端）/ `test_reconnect_does_not_create_second_job`（执行岛） |
| 快照与事件衔接不漏更新 | ✅ | `snapshot` 返回 `event_cursor`；`session_seq > cursor` 订阅 |
| Executor 无法读取模型 API Key；密钥不出现在错误返回或事件中 | 🟡 | 架构上保证（Executor 不加载控制端配置、`.env` 里没有模型密钥、命令环境走白名单）；**未做**针对性的渗透式验证 |

---

## P2 · 第一条真实 Agent 纵向闭环

依赖：P1。目标：任务创建 → 模型 → 执行岛 HTTP → 证据 → 回答 → 继续对话。

| 任务 | 状态 | 证据 / 说明 |
|---|---|---|
| `agent/factory.py` 唯一入口，接入真实模型、HTTP 工具、证据读取、计划更新 | ✅ | `agent/factory.py` + `agent/middleware/*`（6 个中间件） |
| session → thread_id 稳定映射；run/segment 明确区分 | ✅ | `thread_id` 由 SessionRepository 生成且唯一；`run.segment_index` 与 `_run_segments` |
| 独立 Runner 进程：取得 lease、消费 command、驱动图、记录完成/失败 | ✅ | `runtime/runner.py`；`uv run workbench runner` |
| API 只写 command 返回 202；不在 FastAPI 请求或 SSE generator 中驱动 Agent | ✅ | `api/routes.py`（`202`）+ `api/sse.py`（**只读数据库轮询**） |
| basic context_builder：目标、近期消息、计划、证据引用；不做机械条数截断 | ✅ | `agent/middleware/context_builder.py` + `context_manifest` |
| 模型用量从第一条真实请求开始记录；无价格也保存 tokens 与 unpriced | ✅ | 实测 4 次调用入库：`usage_status=reported`、`cost_status=unpriced`，tokens 齐全 |
| 最小 SSE：模型输出、工具状态、run 状态、错误 | ✅ | `api/sse.py` + `contracts/events.py`（19 种事件类型） |
| 用户补充消息排队与消费；未闭合工具消息组不被插入破坏 | ✅ | `runtime/commands.py` + `CommandGuardMiddleware.abefore_model`（一致边界注入） |
| 阶段结束与再次发言：新 run，旧 session/thread 不变 | ✅ | `test_new_run_after_completion_keeps_same_thread` |
| 框架递归/步数异常处理，不冒充 completed | ✅ | `runner._classify_exception` → `RECOVERING` + 明确说明 |
| 最简 UI 或 CLI 观察器完成链路 | ✅ | `workbench runner --once`、`workbench export-run`、`GET /api/sessions/{id}/snapshot`、`/events` |

**P2 验证清单复核**

| 项 | 状态 | 证据 |
|---|---|---|
| 真实小米模型读取本地可控目标，产出带实际证据引用的回答 | ✅ | 端到端冒烟：`run status=completed / completion_reason=goal_answered`；2 个工件（`available`，152 B + 751 B）；1 条 observation 引用两个真实 `artifact_id`；1 条 coverage `info_disclosure/checked_negative` |
| 关闭页面再打开，任务继续且可回放输出 | ✅ | 事件持久化在 `events` 表；SSE 只轮询数据库，UI 断开不阻塞 Runner（架构保证 + `test_committed_events_are_replayable_after_broadcast_loss`） |
| 完成后追问先前观察，能定位原始证据 | ✅ | `read_artifact` 按 `artifact_id` 读脱敏视图；`search_evidence` 检索清单 |
| API 重启不终止 Runner；Runner 重启后恢复或明确 recovering，不重复执行工具 | 🟡 | `recover_stale_runs` + `reconcile_remote_jobs` 有代码与单测（`test_stale_running_run_is_requeued_not_failed`、`test_active_lease_run_is_not_stolen`）；**未做**进程级 kill/restart 的实机演练 |
| 启动第二个 Runner 不获得同 session 执行权 | ✅ | `test_lease_conflict_blocks_second_runner` |

---

## P3 · 后台命令、进程控制与完整 Executor

| 任务 | 状态 | 说明 |
|---|---|---|
| Executor 常驻，SSH 断开不销毁进程服务 | ✅ | `setsid nohup` + PID 文件；见 decision 0002 |
| `start/get/output/cancel` job 接口与远端事件补读 | ✅ | `executor/api.py` 全部实现；`GET /events?after=` 补读 |
| `start_command` 返回 job_id；stdout/stderr 增量保存带 cursor | 🟡 | 代码完整（`processes.py` 分段读 + 不透明 cursor）；**未实机验证**慢命令的三段输出 |
| 任务工作目录与文件 read/write/edit | ⬜ | 未实现（Executor 有 workspace 目录，无文件工具接口） |
| 进程组启动、超时、取消升级、子孙回收、PID 身份核对 | 🟡 | `processes.py` 实现完整（SIGTERM→SIGKILL、`/proc/<pid>/stat` start_ticks）；**未实机验证**子孙进程回收 |
| 工具资源锁：同文件/同身份写操作串行；独立任务有界并发 | ✅ | `tools/runtime.py` + 单测 `test_file_lock_serializes_shared_state` |
| 网络范围的执行环境级约束及能力预检 | ⛔ | **明确关闭并如实暴露**：`network_isolation=unavailable`，`command_unattended_allowed=false` |
| minimal environment allowlist | ✅ | `processes.minimal_environment`（白名单 + 显式屏蔽管理类秘密） |
| `waiting_tool` 事件唤醒，不持续询问模型 | 🟡 | 长任务返回 `accepted` + job_id（超阈值路径已实现）；**未验证**大规模并发下的唤醒时序 |
| 暂停与取消不同语义；暂停后活动工具仍在运行时明确展示 | ✅ | `pause.md`/`cancel` 分离；`_cancel_active_jobs` 逐个核对并写 `process_confirmed_exited` |
| 断线/重启核对所有已接收 job；不确定标 unknown，不自动重做 | ✅ | `reconcile_remote_jobs`；单测 `test_executor_restart_marks_unresolved_jobs_unknown` |
| 远端工件同步、哈希核对、缺失状态与断点重试 | ✅ | 哈希不匹配 → `orphaned`（不作为可用证据）；`flag_orphan_artifacts` 单测 |

---

## P4 · 浏览器、登录与 HTTP 身份会话

| 任务 | 状态 |
|---|---|
| Playwright 浏览器构建与系统依赖 | ⬜（`playwright` 包已装，浏览器二进制未装） |
| BrowserContext 管理、导航、snapshot、交互、网络记录、截图、下载 | ⬜ |
| 页面/浏览器崩溃单独报告与恢复 | ⬜ |
| 每身份 HTTPX AsyncClient / Cookie jar | ✅ `http_sessions.py`（按身份隔离，跨身份不共享 Cookie） |
| 凭据 `secret_ref` 注入、认证状态受限存储、登录探针 | ⬜（`secret_ref` 解析已实现，登录探针未实现） |
| Cookie/LocalStorage/IndexedDB/sessionStorage 恢复支持范围声明 | ⬜ |
| 浏览器到 HTTP 的受控会话转换 | ✅ `export_cookies` / `import_cookies`（保留域/路径/Secure 属性） |
| CSRF/短时 token 的获取来源与刷新 | ⬜ |
| 控制机本地网站到执行岛的目标映射与 Host/SNI 检查 | ✅ `policy.TargetMapping`（显式映射 + `Host`/`SNI`）+ 逐跳范围检查 |
| doctor 从执行端检查 DNS/TLS/HTTP；明确代理继承 | 🟡 `/health`、`/capabilities` 有效；`trust_env=False` 明确关闭隐式代理；**未实现**独立 DNS/TLS 探测命令 |
| 范围逐跳验证重定向及第三方资源 | ✅ `http_sessions` 重定向每跳调用 `policy.check` |

**P4 整体：未交付**。Executor 对 `/browser-sessions` 返回 **501**，不是假实现。

---

## P5 · 任务记忆、压缩恢复与跨任务经验

| 任务 | 状态 | 说明 |
|---|---|---|
| 结构化 `work_summary` 与 `context_manifest` | ✅ | `agent/state.py` + `context_builder.py` |
| 接入官方摘要中间件；领域模板 + 完整性校验 | 🟡 | 中间件接入 + 中文领域模板 + `CompactionValidatorMiddleware`（校验目标/约束/摘要来源区间）；**未实测**两次压缩后的信息保全 |
| 超长工具结果自动落工件，提供摘要与 `read_artifact` | ✅ | 超 `inline_output_limit_bytes` 时工件保留 + 摘要 |
| 上下文容量按模型配置，给输出预留；token 估算与报告用量分开 | ✅ | `input_budget_tokens = context_limit - output_reserve`；manifest 标注 `chars_div_4` 且 `token_estimate_is_exact=false` |
| 压缩前后保留来源范围、摘要版本、回退记录 | ✅ | `memory.compacted` 事件含 `from/to_message_count`、`summary_version`、`problems`；旧 checkpoint 可回退 |
| 摘要失败不删除旧上下文；记录明确错误并保持可恢复 | 🟡 | 校验失败时不删除任何消息、写 `finish_gate_feedback`；**未做**摘要模型故障注入实测 |
| Store 按项目/方法隔离，`candidate`/`verified`、来源、有效期、冲突处理 | ✅ | `memory/store.py`；冲突时**不覆盖**已验证条目，另存候选并保留来源 |
| 阶段收尾提炼经验；运行中按需检索 | 🟡 | `propose_memory` / `search_memory` 实现；阶段收尾自动提炼 ⬜ |
| 用户原始目标、指令、挂起工具、未验证线索不依赖自然语言摘要 | ✅ | 这些是**结构化状态字段**，不是摘要文本 |
| 新任务不自动继承旧任务账号 Cookie | ✅ | 身份与 `auth_revision` 按项目隔离；HTTP 会话按 identity 分桶 |

**P5 记忆专项评测：未执行**（需要长任务 + 两次压缩 + 中途重启的实机场景）。

---

## P6 · 探索、线索、验证、覆盖与报告

| 任务 | 状态 | 说明 |
|---|---|---|
| assets / leads / findings / verifications / coverage / report_versions 表与 API | 🟡 | 表 ✅、领域服务 ✅、工具 ✅；`report_versions` 只有表，报告 API 返回 **501** |
| 计划/线索工具，目标与计划可修改，保持版本与理由 | ✅ | `update_plan`（写图状态）、`record_lead`（含 `priority_rationale`） |
| 线索优先级考虑影响/证据/可执行性/信息增益 | ✅ | `domain/leads.compute_priority`（四维加权 + 分项可解释） |
| 覆盖记录明确资产/身份/检查类型；未检查/受阻/阴性/阳性分开 | ✅ | `CoverageService`（组合键去重 + `ratio_definition` 说明分母） |
| 独立核验器；模型提出发现不直接 confirmed | ✅ | `VerificationService.record`：非 `performed_by=code` 不能 confirmed；DB CHECK 兜底 |
| `propose_completion` 与 `finish_gate`，不加独立评审模型调用 | ✅ | `runtime/stopping.FinishGate`（6 项确定性检查，**不调模型**） |
| 开放探索收尾做覆盖缺口复查 | ✅ | `FinishGate._coverage_gap` |
| 无进展检查去重；区分工具故障/模型重复/有效阴性 | 🟡 | `RunContext.note_progress` + `no_progress_signature`；**未接入** Runner 的收敛判定 |
| 明确目标与开放探索两种停止口径 | ✅ | `CompletionReason.GOAL_ANSWERED` / `EXPLORATION_CONVERGED` |
| 报告快照、Markdown/HTML 模板、引用校验、版本管理、脱敏导出 | ⬜ | **未实现**，API 返回 501 |

---

## P7 · 完整控制台、成本与可观测体验

| 任务 | 状态 |
|---|---|
| 项目列表、任务对话、发现证据、后台任务、成本时间线、配置页面 | ⬜ |
| OpenAPI 生成 TS 类型 | 🟡 FastAPI 自动产出 `/openapi.json`；前端未建立 |
| SSE 断线续读、去重、游标过期重拉快照、心跳、断线提示 | 🟡 服务端 ✅（`Last-Event-ID`/`after`、resync 帧、心跳、`session_seq` 排序）；前端 ⬜ |
| 消息/工具日志分块与虚拟列表 | ⬜ |
| 区分模型等待、工具执行、远端断线、验证、需要输入 | 🟡 `spans` 表 + `Phase` 枚举已定义 14 个阶段；UI ⬜ |
| 费用价格配置、Decimal 计算、输入/输出/缓存拆分、未知请求数 | 🟡 计算与拆分 ✅（单测覆盖）；价格配置 UI ⬜ |
| 最终用量替代流式估计，不累加两次；多币种分开显示 | ✅ `merge_usage_update` 语义 + `summarize_costs` 分币种（单测覆盖） |
| 显示 plan / coverage / lead / finding 状态；React Flow 仅展示关系 | ⬜ |
| 敏感证据安全预览、下载权限、报告版本选择 | 🟡 `GET /api/artifacts/{id}` 提供脱敏预览 ✅；下载权限与报告版本 ⬜ |
| 设置显示生效范围（下个 run / 当前 run） | ✅ `GET /api/settings/models` 返回 `effective_scope` |

**前端整体未建立**。当前可观测手段是 CLI（`workbench runner --once`、`doctor`、
`export-run`）与直接调用 API。

---

## P8 · 故障恢复、边界与发布加固

| 任务 | 状态 |
|---|---|
| 模型请求中断、派发前/后崩溃、checkpoint 前崩溃的故障注入 | 🟡 数据库层切点已覆盖（`tests/recovery/` 12 项）；**进程级** kill 演练 ⬜ |
| 旧 run owner 迟到响应不再派发，费用仍记录 | ✅ `test_late_owner_response_is_not_dispatched` + `ToolDispatchMiddleware` 所有权/版本复核 |
| Executor/control 重启、数据库不可用、磁盘满、工件同步失败 | 🟡 Executor 重启核对 ✅；数据库不可用 / 磁盘满 ⬜ |
| interrupt 恢复前重复运行是否含非幂等副作用 | ✅ 不适用 `interrupt()`，走状态机（见 P0 偏离说明①） |
| 密钥脱敏、提示注入资料边界、HTML/终端控制字符安全显示 | ✅ `redact_text` 先处理 Bearer 再按键值打码（曾抓出真实泄漏）；控制字符剥离；注入诱饵已在 fixture 中（`/content/notice`） |
| 范围变更后旧 job / 旧提议行为 | 🟡 `control_version` 递增 + 派发前复核 ✅；旧 job 处置 ⬜ |
| 数据保留、报告引用保护、备份与恢复演练 | 🟡 `workbench backup`（pg_dump + 工件 + MANIFEST）✅；**未做**恢复演练 |
| 升级时检查 state/协议/schema 版本 | 🟡 `state_schema_version`、`protocol_version`、`capabilities_version` 已定义并比对（协议不兼容直接报错）；迁移脚本 ⬜ |
| 导出诊断包 | ✅ `workbench export-run`（脱敏，不含密钥与正文） |
| 旧代码迁移来源与许可证清单 | ✅ **未迁移任何旧代码**，因此无来源清单需要整理 |

---

## P9 · 真实效果评测、交接与后续落地

| 任务 | 状态 |
|---|---|
| ≥8 个有独立判据的本地可控场景 | 🟡 `evals/fixtures/target_app.py` 已预置 E01/E03/E04/E05/E06/E08/E10 的场景端点；场景定义与 grader ⬜ |
| 真实小米模型重复运行、保存原始轨迹 | ⬜（本轮只做了单次冒烟） |
| 与旧 B1 配对基线 | ⬜ |
| 每个 trial 重置环境；模型不可见参考答案/fixture 源码/grader | 🟡 fixture 源码只放在执行岛执行目录，Agent 只能经网络访问 ✅；重置命令 ⬜ |
| 报告正确率、完成、重复操作、人工介入、框架等待、费用 | ⬜ |
| 干净环境按 README 部署并跑通 | 🟡 本机按 README 部署并跑通 ✅；**未在**全新机器复现 |
| 交接材料 | 🟡 本文档 + decisions + README；脱敏诊断包与真实 run 报告 ⬜ |

---

## 本轮明确"未做"的清单（供后续接手）

1. **前端控制台整体**（P7）。
2. **浏览器能力**（P4）——Executor 如实返回 501。
3. **报告生成**（P6）——API 如实返回 501。
4. **文件 read/write/edit 工具**（P3）。
5. **阶段收尾自动提炼长期经验**（P5）。
6. **无进展收敛接入 Runner**（P6）。
7. **进程级故障注入与恢复演练**（P8）。
8. **备份恢复演练**（P8）。
9. **效果评测 runner 与 grader**（P9）。
10. **systemd/容器级进程守护**（P3）——当前是 `setsid`，机器重启不自动拉起。

## 复现本文件的验证

```powershell
uv sync --frozen --all-packages --group dev
docker compose up -d postgres
uv run workbench db-init
uv run python scripts/check_encoding.py
uv run ruff check . ; uv run pyright
uv run pytest tests/unit tests/integration tests/recovery -q     # 90 passed, 1 skipped（无凭据时）
uv run workbench doctor
```

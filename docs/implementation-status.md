# 实现状态（P0–P9）

> 本文件是 `IMPLEMENTATION_CHECKLIST.md` 在本仓库的落地记录。
> **只有具备验证证据的条目才标 ✅**；未验证的一律标 ⬜ / ⛔，不用"已支持"文案。

- 记录日期：2026-10-10（P0–P2 复核，P3–P5 执行岛切片，P6 受限核验器与 P7 耗时诊断切片）
- 代码版本：见 `git log`（本仓库自身版本；`run.code_version` 记录的是运行时的代码标识）
- 控制端环境：Windows，CPython 3.12.14（uv 管理），PostgreSQL 16.4（Docker，端口 55432）
- 执行岛：Kali `192.168.72.134`，Python 3.13.12，Executor 装于
  `~/rexak-agent-executor`，数据根 `~/rexak-agent-data`
- 真实模型：小米 `mimo-v2.6-pro` @ `https://api.xiaomimimo.com/v1`

## 状态图例

> 2026-10-13：[V1 六阶段代码](implementation-v1-progress.md) 已接通，包括完整请求预算、压缩观测、主动分段、只读黑板与焦点、历史检索、经验隔离与阶段报告，以及实验准备和离线分析。最终回归、限制和启用步骤以进度页与 [操作说明](experiments/v1-operation-guide.md) 为准；P0–P9 的历史结果不能冒充本轮 V1 效果验收。

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
| 锁 Python / Node / pnpm 版本 | 🟡 | Python ✅（`.python-version`=3.12 → 3.12.14）；前端 `packageManager` 锁 pnpm 11.22.0、`pnpm-lock.yaml` 锁依赖，`engines.node` 仅规定最低版本，尚未锁定 Node 的精确版本 |
| Python dev group、前端 pnpm-lock、Ruff/Pyright/ESLint/TS、Vitest | 🟡 | Python dev group、pnpm-lock、Ruff、Pyright、TypeScript 类型检查 ✅；ESLint、Vitest 尚未配置 |
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
| 工具异常与远端 404 不得中断恢复流程 | ✅ | `ExecutorRejected` 被显式分类；404 -> `unknown`（不推断为"没发生副作用"）；单 job 意外只影响该 job。`tests/recovery/test_reconcile_failures.py` |

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
| API 重启不终止 Runner；Runner 重启后恢复或明确 recovering，不重复执行工具 | ✅ | **进程级演练通过**（`scripts/restart_drill.py`，见 decision 0003 §2）：硬杀 Runner 后 lease 过期 -> 核对 -> 明确决定归宿 -> 目标端 `/slow` 只执行 **1** 次 |
| 启动第二个 Runner 不获得同 session 执行权 | ✅ | `test_lease_conflict_blocks_second_runner` |
| **方向变更可靠拦截旧模型响应** | ✅ | `command_guard` 在模型请求前固定 `control_version`；`tool_dispatch` 用固定值与当前值比较，缺失时 fail-closed；`tests/integration/test_control_version.py` 用"模型生成期间真的改库"的真实时序验证（见 decision 0003 §1） |
| **验收命令可原样复现** | ✅ | `uv run pytest tests/unit tests/integration -q` 通过；`uv run workbench doctor` 在 cp936 控制台下也不崩溃（见 decision 0003 §3） |

**P2 验收结论：本轮修复了独立复核指出的两处运行边界缺陷与一处命令可复现性问题，
并补了真实并发测试与进程级演练。** 未完成项见文末"本轮明确未做的清单"。

---

## P3 · 后台命令、进程控制与完整 Executor

| 任务 | 状态 | 说明 |
|---|---|---|
| Executor 常驻，SSH 断开不销毁进程服务 | 🟡 | 现有 `setsid nohup` 链路已验证；新增 `deploy/rexak-executor.service` systemd 模板，尚未在 Kali 安装演练 |
| `start/get/output/cancel` job 接口与远端事件补读 | ✅ | `executor/api.py` 全部实现；`GET /events?after=` 补读 |
| `start_command` 返回 job_id；stdout/stderr 增量保存带 cursor | ✅ | 岛上实机验证：三段输出的慢命令**退出前**即可读到第一段（此时进程仍在运行），游标分段读拼回完整输出逐字节一致；stdout/stderr 不串流。**模型侧仍不可用**：无人值守命令保持关闭，见下条 |
| 任务工作目录与文件 read/write/edit | ✅ | `executor/.../filesystem.py` + `tools/files.py`（4 个工具）。只接受 UUID 形式的 `workspace_id`；三层路径检查挡住 `..`、绝对路径与**软链接逃逸**；覆盖/编辑必须先读后改（内容 sha256 版本）。单测 20 项 + 岛上实机 20 项 |
| 进程组启动、超时、取消升级、子孙回收、PID 身份核对 | ✅ | 岛上实机验证（`scripts/executor_process_check.py`，10 项）：取消产生子进程的命令后，用 `/proc` **独立核对**子进程确实退出；重复取消幂等；超时后进程被真正终止；PID start_ticks 能识别退出，缺 ticks 时不误判存活 |
| 工具资源锁：同文件/同身份写操作串行；独立任务有界并发 | ✅ | `tools/runtime.py` + 单测 `test_file_lock_serializes_shared_state` |
| 网络范围的执行环境级约束及能力预检 | ⛔ | **明确关闭并如实暴露**：`network_isolation=unavailable`，`command_unattended_allowed=false`。即便环境变量写 `enforced`，没有实际网络隔离实现也不会解锁命令；有回归测试 |
| minimal environment allowlist | ✅ | `processes.minimal_environment`（白名单 + 显式屏蔽管理类秘密） |
| `waiting_tool` 事件唤醒，不持续询问模型 | ✅ | `runtime/jobs.py` 唤醒器 + 13 项测试。等待中且无待消费指令的 run **不会**被 `_candidate_runs` 取走（机制性证明不空转）；**`unknown` 不是确定结论**，不据它唤醒；执行岛不可达时保持等待且不注入消息 |
| 暂停与取消不同语义；暂停后活动工具仍在运行时明确展示 | ✅ | `pause.md`/`cancel` 分离；`_cancel_active_jobs` 逐个核对并写 `process_confirmed_exited` |
| 断线/重启核对所有已接收 job；不确定标 unknown，不自动重做 | ✅ | `reconcile_remote_jobs`（有界并发，`JOB_RECONCILE_CONCURRENCY=8`）；单测 `test_executor_restart_marks_unresolved_jobs_unknown`；**进程级演练通过** |
| 远端工件同步、哈希核对、缺失状态与断点重试 | 🟡 | 分块按 offset 续取，将部分文件与哈希元数据持久落盘；中断后从既有 offset 恢复的单测通过，整文件 SHA-256 不符不作为证据。尚未做进程级中断演练 |

---

## P4 · 浏览器、登录与 HTTP 身份会话

| 任务 | 状态 | 证据 / 限制 |
|---|---|---|
| Playwright 浏览器构建与系统依赖 | ✅ | 本机 Edge 与 Kali Chromium 均已启动并通过浏览器专项测试；Kali 执行岛全套 42 项通过 |
| BrowserContext 管理、导航、snapshot、交互、网络记录、截图、下载 | ✅ | 本机 Edge 与 Kali Chromium 实测；远端 `executor/tests` 42 项通过 |
| 页面/浏览器崩溃单独报告与恢复 | 🟡 | 断开/关闭分别返回 `browser_crashed` / `page_crashed`；下次调用可从状态文件重建，登录仍须探针确认 |
| 每身份 HTTPX AsyncClient / Cookie jar | ✅ | `HttpSessionRegistry` 在 Executor 进程常驻；跨 job 同身份 Cookie 保留，异身份测试 401 |
| 凭据 `secret_ref` 注入、认证状态受限存储、登录探针 | 🟡 | 控制端 `browser_login` 解析 `secret_ref`；执行岛表单登录与探针在本机和 Kali 均实测；SQLite 台账脱敏。控制端到真实模型任务的完整链路未跑 |
| Cookie/LocalStorage/IndexedDB/sessionStorage 恢复支持范围声明 | ✅ | 前三者使用 `storage_state(indexed_db=True)`；sessionStorage 不保证恢复，见 `docs/p3-p5-deployment.md` |
| 浏览器到 HTTP 的受控会话转换 | ✅ | 实机测试浏览器登录后同身份 HTTP 200，其他身份 401；域、路径、Secure 保留 |
| CSRF/短时 token 的获取来源与刷新 | 🟡 | HTTP 工具可指定来源 URL 与隐藏 input，每次写请求在同一身份锁内刷新并注入 form/json/header；本机与 Kali fixture 实测。Cookie/meta/JS 派生 token 尚未覆盖 |
| 控制机本地网站到执行岛的目标映射与 Host/SNI 检查 | 🟡 | 显式 HTTP 映射下浏览器登录和同身份 HTTP 会话已验证；跨主机 HTTPS SNI 不保证，探针明确返回 `unsupported` |
| doctor 从执行端检查 DNS/TLS/HTTP；明确代理继承 | 🟡 | `/target-probe` 与 `doctor --target-url --scope-file` 已在 Kali 对本地 fixture 验证 DNS/HTTP 200；HTTPS 未实测；代理不继承 |
| 范围逐跳验证重定向及第三方资源 | 🟡 | HTTP 重定向逐跳检查、浏览器 context 逐请求阻断子资源与 WebSocket；动态 DNS 地址约束尚需内核级隔离 |

**P4 当前为执行岛可运行切片**；真实模型调用、HTTPS SNI 映射与非隐藏表单型 token 自动刷新尚未完成。

---

## P5 · 任务记忆、压缩恢复与跨任务经验

| 任务 | 状态 | 说明 |
|---|---|---|
| 结构化 `work_summary` 与 `context_manifest` | ✅ | `agent/state.py` + `context_builder.py` |
| 接入官方摘要中间件；领域模板 + 完整性校验 | 🟡 | `GuardedSummarizationMiddleware` 在提交删除前校验并固定目标/约束/挂起 job/未完线索；假模型两次压缩与 PostgreSQL 重开连接测试通过，真实小米长任务未测 |
| 超长工具结果自动落工件，提供摘要与 `read_artifact` | ✅ | 超 `inline_output_limit_bytes` 时工件保留 + 摘要 |
| 上下文容量按模型配置，给输出预留；token 估算与报告用量分开 | ✅ | `input_budget_tokens = context_limit - output_reserve`；manifest 标注 `chars_div_4` 且 `token_estimate_is_exact=false` |
| 压缩前后保留来源范围、摘要版本、回退记录 | ✅ | `memory.compacted` 事件含 `from/to_message_count`、`summary_version`、`problems`；旧 checkpoint 可回退 |
| 摘要失败不删除旧上下文；记录明确错误并保持可恢复 | 🟡 | 失败抛 `CompactionFailed`，Runner 进入 recovering；假模型失败注入证明旧消息未删除，进程级故障未演练 |
| Store 按项目/方法隔离，`candidate`/`verified`、来源、有效期、冲突处理 | ✅ | `memory/store.py`；冲突时**不覆盖**已验证条目，另存候选并保留来源 |
| 阶段收尾提炼经验；运行中按需检索 | 🟡 | 观察现绑定 `source_run_id`；完成 run 后只从本 run 的可用工件观察提炼项目 `candidate`，PostgreSQL 集成测试通过。真实模型端到端未测 |
| 用户原始目标、指令、挂起工具、未验证线索不依赖自然语言摘要 | ✅ | 这些是**结构化状态字段**，不是摘要文本 |
| 新任务不自动继承旧任务账号 Cookie | 🟡 | Executor 身份键包含 session ID；同 session 跨 run 延续，另一 session 不共用；仍需跨进程实测 |

**P5 记忆专项评测：确定性两次压缩和 PostgreSQL 重开连接通过；真实小米长任务、挂起 job 同时压缩与进程重启场景仍未执行。**

---

## P6 · 探索、线索、验证、覆盖与报告

| 任务 | 状态 | 说明 |
|---|---|---|
| assets / leads / findings / verifications / coverage / report_versions 表与 API | 🟡 | 表、领域服务和工具已接入；资产、线索、发现、验证记录、覆盖摘要与报告版本的只读 API 已验证，报告创建/导出 API 已验证；领域记录的写入仍主要经 Agent 工具，未做完整控制台 |
| 计划/线索工具，目标与计划可修改，保持版本与理由 | ✅ | `update_plan`（写图状态）、`record_lead`（含 `priority_rationale`） |
| 线索优先级考虑影响/证据/可执行性/信息增益 | ✅ | `domain/leads.compute_priority`（四维加权 + 分项可解释） |
| 覆盖记录明确资产/身份/检查类型；未检查/受阻/阴性/阳性分开 | ✅ | `CoverageService`（组合键去重 + `ratio_definition` 说明分母） |
| 独立核验器；模型提出发现不直接 confirmed | 🟡 | `verify_public_git_head` 已接入工具注册表：先校验候选的原始 HTTP 工件，再以隔离身份发起新的 GET；代码核对两个操作、响应格式与文件哈希后才写确认记录，并把结论限定为 Git HEAD 引用可读。集成测试覆盖成功、伪证据、重复操作、文件篡改与报告引用；真实小米任务及执行岛端到端复测尚未做，其他漏洞类型仍保持 candidate |
| `propose_completion` 与 `finish_gate`，不加独立评审模型调用 | ✅ | `runtime/stopping.FinishGate` 不调模型；已确认发现逐个核对独立验证记录与工件哈希 |
| 开放探索收尾做覆盖缺口复查 | ✅ | `FinishGate._coverage_gap`；零登记覆盖不能宣称收敛，重点线索仅设为 deferred 不能绕过检查 |
| 无进展检查去重；区分工具故障/模型重复/有效阴性 | 🟡 | `ProgressMiddleware` 按去重证据、入口、线索、覆盖、验证计算签名；连续 3 轮提示换路线，8 轮在模型调用前暂停并保留任务。假模型 + PostgreSQL checkpoint 已验证，真实模型效果待评测 |
| 明确目标与开放探索两种停止口径 | ✅ | `CompletionReason.GOAL_ANSWERED` / `EXPLORATION_CONVERGED` |
| 报告快照、Markdown/HTML 模板、引用校验、版本管理、脱敏导出 | ✅ | `domain/reports.py` 两段短事务发布版本；确认项要求独立验证、同任务工件、未截断且文件哈希通过；旧版本保留、报告和证据受保护；API 集成测试通过 |

---

## P7 · 完整控制台、成本与可观测体验

| 任务 | 状态 |
|---|---|
| 项目列表、任务对话、发现证据、后台任务、成本时间线、配置页面 | 🟡 React 控制台已建并通过 TS/build 与本地浏览器检查；项目/任务创建、消息与运行控制、发现证据、任务、费用、报告、模型设置均有界面。对话归档已接入，真实模型交互验收仍缺 |
| OpenAPI 生成 TS 类型 | ✅ `scripts/export_openapi.py` + `web` 的 `generate:api` 生成契约类型；事件历史页与 SSE 共用 `EventEnvelope` schema；`pnpm build` 通过 |
| SSE 断线续读、去重、游标过期重拉快照、心跳、断线提示 | 🟡 服务端具备续读/resync/心跳；前端按 `session_seq` 去重、断线提示、resync 重拉，且定期核对服务端游标，发现静默漏收会重新订阅。集成演练覆盖断开后补读与游标过期提示；本机 Chromium 主动关闭 SSE 后补读通过。新模型输出在写入事件前保留分片尾部并延迟敏感字段，旧原始分片在浏览器接口隐藏；静态规则之外的任意凭据、跨模型调用边界和真实网络故障仍未验证 |
| 消息/工具日志分块与虚拟列表 | 🟡 事件历史支持前后逐页浏览，浏览旧页时实时流继续接收；每页最多 100 条，固定高度行只渲染可见范围，详情按序号从脱敏接口读取。前端最多保留 200 条实时事件和最近 20k 字符助手输出。用户初始目标与补充指令从业务表分页读取，模型文本在每次物理调用完成后保存为不可变工件并关联模型台账；对话正文按完整脱敏视图分段读取。集成测试覆盖写入、分页、隔离和脱敏；旧模型调用不回填，真实模型长任务界面仍待验收 |
| 区分模型等待、工具执行、远端断线、验证、需要输入 | 🟡 控制端时间线显示模型调用、工具操作和下一次模型请求间隔；图流分片与模型最终消息 ID 精确匹配时写入首个可见输出时间，失配保持未知。Executor 接收/开始/结束时间已投影到 job 并分别显示排队与执行区间，缺项显示未知。结束提议的中间件检查、Runner 复核和报告生成总耗时各有独立 span；报告另有快照采集、证据校验、正文渲染、版本发布四段，成功、校验/发布失败及发布取消均经数据库测试；成功路径另经只读 API 测试。Git HEAD 核验工具新增预检查、复测调用返回、结论写入三段 span，成功、受理后等待及证据不符路径由数据库测试覆盖；复测被受理后的远端排队和执行时间仍以 job 台账为准。首个可见分片不等于供应商首字节，真实小米/Kali 时间线仍待验收 |
| 费用价格配置、Decimal 计算、输入/输出/缓存拆分、未知请求数 | 🟡 服务端价格/Decimal 计算已验证；控制台显示分币种费用、未定价和未知调用、各类 token 与模型调用时间线。价格编辑写入数据库中的不可变目录版本，乐观版本冲突返回 409；首次启动时将版本固定到 run，同一 run 恢复后仍使用该版本，旧 run 可从既有模型调用回填。接口鉴权、版本冲突、恢复口径与数值计算有自动测试；真实小米账单对照仍由用户验收 |
| 最终用量替代流式估计，不累加两次；多币种分开显示 | ✅ `merge_usage_update` 语义 + `summarize_costs` 分币种（单测覆盖） |
| 显示 plan / coverage / lead / finding 状态；React Flow 仅展示关系 | ✅ 控制台显示 coverage/lead/finding 和当前 plan 详情；计划从 LangGraph checkpoint 只读获取，React Flow 只连接计划中明确填写的 `lead_id`，不推断执行依赖。checkpoint 读取、会话隔离和脱敏有集成测试，前端构建通过；真实长任务的视觉验收仍待用户测试 |
| 敏感证据安全预览、下载权限、报告版本选择 | 🟡 前端已接脱敏工件预览与报告版本下载；更细的导出权限尚未设计，原件不经预览接口下载 |
| 设置显示生效范围（下个 run / 当前 run） | ✅ `GET /api/settings/models` 返回 `effective_scope` |

前端当前是 **P7 的基础控制台切片**，不是完整 P7 验收。`/observer` 仍保留为只读诊断页。

---

## P8 · 故障恢复、边界与发布加固

| 任务 | 状态 |
|---|---|
| 模型请求中断、派发前/后崩溃、checkpoint 前崩溃的故障注入 | 🟡 数据库层切点已覆盖（`tests/recovery/`）；进程级杀死 run owner 后，租约未过期不接管、到期后只进入一次 recovering 的演练通过。真实模型调用中断与 checkpoint 切点的进程级演练仍缺 |
| 旧 run owner 迟到响应不再派发，费用仍记录 | ✅ 迟到响应用例已改为**调用真实派发路径** `_assert_dispatch_allowed`；另有「用户改变方向 -> `StaleResponseError`」用例 |
| Executor/control 重启、数据库不可用、磁盘满、工件同步失败 | 🟡 Executor 重启核对 ✅；数据库不可达端口注入证明 Runner 不会虚报 run 失败或产生工具操作，真实数据库停机/恢复演练仍缺；工件正文和元数据发布阶段模拟 `ENOSPC`，均不会假报成功，临时文件清理并可重试；远端工件分块同步中断后可按已存偏移续传。真实磁盘满演练仍缺 |
| interrupt 恢复前重复运行是否含非幂等副作用 | ✅ 不适用 `interrupt()`，走状态机（见 P0 偏离说明①） |
| 密钥脱敏、提示注入资料边界、HTML/终端控制字符安全显示 | ✅ `redact_text` 先处理 Bearer 再按键值打码（曾抓出真实泄漏）；控制字符剥离；注入诱饵已在 fixture 中（`/content/notice`） |
| 范围变更后旧 job / 旧提议行为 | 🟡 方向变更重试只递增一次 `control_version`；显式 `PUT /api/sessions/{id}/scope` 验证修订并原子更新范围与控制版本，下一模型边界刷新工具箱范围。旧模型提议派发前拦截；已开始的工具若在方向变更后返回，其正文不注入当前决策，远端副作用保留待核对。已提交旧 job 继续查询真实状态、不盲目取消，终态唤醒与模型主动读取均标记为历史结果；迁移前旧操作版本标为 `unknown`。查询和取消按 run 限定。真实 Executor 长任务与范围变更并发的端到端演练仍待做 |
| 数据保留、报告引用保护、备份与恢复演练 | 🟡 `workbench backup` 支持本机 pg_dump 或显式指定 PostgreSQL 容器，产出数据库、工件和 SHA-256 文件清单；`verify-backup` 可离线发现文件损坏。`scripts/backup_restore_drill.py` 在两份隔离临时数据库中实测备份、还原、中文记录和工件正文一致，并验证篡改会被拒绝。报告发布时已对引用证据与报告文件设置保护标记；`prune-events` 默认预览，仅显式 `--apply` 批量删除过期 SSE 事件，跳过有活动 run 的任务并保留最新游标锚点；集成测试核对过期游标重拉、报告版本与原件不受影响。工件目录本身未启用自动清理；真实业务库异机还原仍缺 |
| 升级时检查 state/协议/schema 版本 | 🟡 Runner 恢复前校验 checkpoint 的 `state_schema_version`，旧版缺失或不匹配时拒绝执行；checkpoint 读取异常不再当作空历史。API/Runner 启动前只读比对业务库 Alembic revision 与代码迁移图 head，`doctor` 同步报告不匹配；Executor `protocol_version` 不兼容会拒绝连接，`capabilities_version` 仍仅展示。当前只有 state v1，尚无跨版本状态迁移脚本；未来升级 v2 前必须提供显式迁移与隔离还原演练 |
| 导出诊断包 | 🟡 `workbench export-run` v2 已导出真实 Earth run；按 run 分页收集保留事件、模型/操作/job/span/报告/观察与工件完整性，领域知识显式标为当前会话快照；附 SHA-256 清单。按已知规则脱敏，不能保证识别自由文本中的未知凭据；不含完整模型请求、checkpoint 或工件正文 |
| 旧代码迁移来源与许可证清单 | ✅ **未迁移任何旧代码**，因此无来源清单需要整理 |

---

## P9 · 真实效果评测、交接与后续落地

| 任务 | 状态 |
|---|---|
| ≥8 个有独立判据的本地可控场景 | ✅ `evals/scenarios.py` 八个自然目标与 `evals/grader.py` 独立原件判据已接入；合成机制测试通过，不代表模型通过 |
| 真实小米模型重复运行、保存原始轨迹 | 🟡 Earth 自然目标真实任务已有脱敏台账与工件完整性诊断，8 次模型调用、10 次成功 HTTP 操作；仍在 recovering，观察和发现均为 0。重复 trial、完整原始请求/checkpoint 轨迹仍缺，不能认定效果达标 |
| 与旧 B1 配对基线 | 🟡 同协议原始包配对比较已实现；旧 B1 运行适配器与真实配对样本未取得 |
| 每个 trial 重置环境；模型不可见参考答案/fixture 源码/grader | ✅ 独立管理通道、随机凭据、Cookie/审计重置、新项目/会话/身份与私有原件判定；Kali 重置演练通过 |
| 报告正确率、完成、重复操作、人工介入、框架等待、费用 | ✅ 代码与拒绝错误指标口径的测试已实现；未测数据保持未知，重复候选不冒充无效重复率；无真实矩阵结果 |
| 干净环境按 README 部署并跑通 | 🟡 本机按 README 部署并跑通 ✅；**未在**全新机器复现 |
| 交接材料 | ✅ 本文档 + decisions + README + [P9 交接](p9-evaluation.md) 与 [2026-10-10 记录](acceptance-results/2026-10-10/README.md)；真实任务尚无成果报告，不声称已生成 |

---

## 本轮明确"未做"的清单（供后续接手）

1. **真实模型长任务对话归档和首个可见分片验收、其余验证路径的阶段细分、真实 Kali 时间线验收**（P7）。模型正文只从归档版本起保存，旧调用不回填；首分片时间只在消息 ID 可关联时写入。报告总耗时与四个内部阶段、Git HEAD 核验三段已入 span。价格版本编辑、计划详情和只读关系图已实现；服务端断线补读/过期游标及 Chromium 主动断开 SSE 后的补读演练通过。新产生的模型流分片可处理已知敏感字段跨分片，旧原始分片浏览器侧隐藏；未知凭据和真实网络故障演练仍未完成。
2. **HTTPS 跨主机 SNI 映射与真实模型端到端浏览器任务**（P4）。
3. **报告模板的真实任务内容评审与更多核验场景**（P6）——确定性版本链路已通过集成测试。
4. **通用命令的内核级目标出站隔离**（P3）。
5. **真实小米长任务下的记忆与登录链路验收**（P5）。
6. **真实模型下评估无进展阈值与换路线效果**（P6）——Runner 已接入，尚无实测效果数据。
7. **进程级故障注入与恢复演练**（P8）。
8. **真实业务库异机还原与工件生命周期策略**（P8）。隔离数据库及工件的还原演练、文件完整性检查已通过；旧 SSE 事件已有显式保留命令，工件目录未自动清理；未将用户工作库还原到新环境。
9. **真实模型效果与配对基线**（P9）。八个自然目标、目标/身份/项目重置、独立原件判定、重复 trial 调度与离线复核已实现。真实模型八场景各三次与旧 B1 配对包仍未取得；不能将机制测试视为自主测试效果。
10. **systemd 模板在 Kali 上的部署演练**（P3）——模板已加入，机器重启拉起未实测。
11. **通用命令对模型开放**（P3）——进程控制链路已验证，但 `kind=command` 仍然拒绝，
    直到执行环境级出站约束通过验证。

> **2026-10-09 更新**：独立复核指出的三处问题（方向变更拦截、恢复顺序、命令可复现性）
> 已全部修复并有验证证据，详见 `docs/decisions/0003-runtime-boundaries.md`。
> 修复过程中由**真实演练**额外暴露并修掉 4 个缺陷：远端 404 导致核对崩溃、
> 单个 job 异常中断整次核对、核对串行过慢拖垮调度、stale 判定口径与文档不符。

## 复现本文件的验证

```powershell
uv sync --frozen --all-packages --group dev
docker compose up -d postgres
uv run workbench db-init
uv run python scripts/check_encoding.py
uv run ruff check . ; uv run pyright
uv run pytest tests/unit tests/integration tests/recovery executor/tests -q
uv run workbench doctor
```

本轮本机全套（本地 fixture 已启动）：197 passed、23 skipped（无小米凭据；Windows 不跑 Linux 专项）。
Kali 执行岛专项：42 passed、1 个非失败的第三方弃用警告。

只能在 Linux 验证的部分（进程控制、软链接逃逸、工作区文件实机链路）：

```
uv run python scripts/executor_process_check.py   # 同步源码并运行岛上专项；最近一次 42 passed
```

进程级重启演练（需要真实模型与可达执行岛）：

```powershell
$env:XIAOMI_API_KEY = "<本机密钥>"
uv run python scripts/restart_drill.py
```


## 2026-10-10 追加验证

本轮修复单次模型/数据库连接等待边界、定向运行恢复、临时数据库测试隔离及诊断 v2，接入评测轨迹。最终分批回归为单测 100 passed / 2 skipped、数据库集成与恢复 132 passed / 1 skipped；早先卡住及初始化超时的运行不计为通过。详细实测与限制见 [验收记录](acceptance-results/2026-10-10/README.md)。

Earth 自然目标测试尚未完成：10 次成功 HTTP 操作和 25 个哈希一致工件，并未形成观察、覆盖或确认发现；最后模型请求在 300 秒边界后进入恢复。P9 评测机制现已补齐，真实效果仍未通过验收。详见 [P9 交接](p9-evaluation.md)。


本次 P9 工程收尾及最后检查已记录在 [最终交接检查](acceptance-results/2026-10-10/README.md#最终交接检查)。评测代码交付与真实效果验收分开，后者仍未通过。

## 2026-10-11 运行模式调整

个人工作台默认按用户指令自动应用目标，严格范围模式保留为可选项；已补旧任务恢复、运行中目标修订与连接状态检查。行为决策见 [0004](decisions/0004-instruction-mode.md)，验证与边界见 [运行模式验收](acceptance-results/2026-10-11/instruction-mode.md)。此变更不代表真实模型渗透效果通过验收。

暂停续聊及旧客户端空范围兼容已经修复。暂停后可以直接发送新消息或新建聊天，旧 checkpoint 的待执行工具不会在新阶段自动重放。行为见 [0005](decisions/0005-pause-continuation.md)，数据库、官方图和实际浏览器结果见 [暂停续聊验收](acceptance-results/2026-10-11/pause-continuation.md)。

聊天工具活动已加入默认折叠与详情展开，字体已加深。HTTPS 失败工件下载覆盖原错误的问题已修复，新增默认开启、单次可显式关闭的证书校验选项。Kali 已更新，并完成指定目标的 HEAD 复测；详细证据见 [工具进度与 HTTPS 验收](acceptance-results/2026-10-11/tool-progress-https.md)。此次真实测试仅验证工具链，没有重新评测模型渗透效果。

模型请求已改为流式活动续期与独立墙钟保护；补齐小米思考字段保留和工具轮次回传，页面区分等待、思考与生成。纯模型超时支持直接恢复和补充输入，未核实工具任务仍需核对。验证范围、最新真实任务诊断和生效步骤见 [模型超时修复验收](acceptance-results/2026-10-11/model-timeout.md)；未重跑真实模型效果测试。

## 2026-10-14 第二版工程验收

基线 `3cb370e` 的六批实现已经过实际代码复核与补修，覆盖响应恢复额度和实际预算、资产来源、显式焦点、状态迁移、结构化请求、进展信号与阶段报告。工程结果和未验证范围见[第二版工程验收](acceptance-results/2026-10-14/implementation-v2.md)。后续 Kali 部署已完成安装包哈希核对、服务重启、能力检查及本地回显验证，原桌面入口启动链路已验证。真实 S2-045、Tomcat8、Earth 复测由用户执行，工程与部署通过不表示真实测试效果通过。

## 2026-10-14 启动模型选择

原桌面入口加入小米与百炼 `deepseek-v4.1-flash` 选择及本次 Key 输入；修正 API 新任务默认模型与 Runner 实际模型绑定，保留已有 run 的暂停恢复配置。代码回归、前端构建及本机 API 验证通过，本次 Kali SSH 不可达，完整双端启动与真实模型效果未验证。详见[模型选择验收记录](acceptance-results/2026-10-14/bailian-model-selection.md)。

## 2026-10-15 第三版工程与部署验收

用户实现基线 `5da9902` 经逐项复核，补齐完整库存、结束与等待语义、真实弱点关系、条件/证据版本、并发约束、条件变化召回、实际发送游标、多线索派发来源及报告/实验包观测。
官方 `create_agent` 合成闭环、隔离数据库、迁移和恢复回归已验证；系统提示 6、方法资料集 4、状态 schema 4。
本机工作库完成前向约束迁移，Kali 离线重装与服务重启/哈希/能力核对通过，原桌面入口的 API-only 控制台启动通过。
真实 Runner 与靶场效果仍由用户测试；工程通过不表示模型实际测试效果通过。详见[第三版验收记录](acceptance-results/2026-10-14/v3/README.md)。


## 2026-10-15 模型预算与 Web 配置

修正内置模型的上下文/完整输出预算，增加小米三款模型及百炼 Flash 的 Web 设置、Windows 加密 Key 持久化与输入框选择。阶段模型与端点固定，旧阶段恢复时升级错误预算并保留审计。启动器取消终端 Key 交互，缺 Key 的 Runner 等待配置，Windows 自动停止会清理子进程。参数、来源和启用方法见[模型配置说明](model-settings.md)，工程与部署证据见[验收记录](acceptance-results/2026-10-15/model-settings/README.md)。真实模型效果仍待用户复测。


## 2026-10-16 命令执行底座

命令、脚本、PTY、监视、屏幕、批量文件编辑与检索迁入 Python Executor；协议为独立生成的 `/execution/v1`。默认发行 disabled，个人 Kali 显式 trusted；不将工作目录或 accounting_only 声称为系统级隔离。

增加独立包装进程与完成回执、稳定操作身份、分流日志与原始字节快照、会话预算、明确等待、按用途区分待办与保留能力、持续委托和可靠事件交付。资源保留、暂停、取消值班、关闭资源及终止任务分开；PTY 重启后交互丢失明确显示，不自动重建。

实际代码、运行证据、部署与未测边界见[本轮验收](acceptance-results/2026-10-16/execution-runtime/README.md)。真实模型与靶场效果不在本轮验收范围，由用户执行。最新行为以 [decision 0006](decisions/0006-execution-runtime.md) 为准，旧章节的命令禁用结论属于此前版本。

后续补齐 SQLite 台账并发事务、输出游标错误分类、资源/操作/job 终态同步和供应商内容审核拒绝处理。工程回归、双端部署及保留状态核对见[执行链路故障修复](acceptance-results/2026-10-16/execution-runtime/engineering-fixes.md)；真实模型与靶场效果仍未在该修复中复测。

## 2026-10-17 Skill 模块

已实现标准 Skill 包、目录/ZIP 发布、完整内容版本、固定运行目录、五个 Agent 工具、渐进上下文与状态 v5 迁移、Kali 完整包脚本执行、技能库页面及管理 API/CLI。普通操作的指导关联、脚本归因和私有运行包中的固定 Skill 依赖可复盘。旧 `search_methods` 保留。

操作方法及第一版限制见 [Skill 模块说明](skills.md)，回归、实际浏览器、Kali 哈希/能力/无害脚本执行和原桌面 API-only 启动链证据见[Skill 验收记录](acceptance-results/2026-10-17/skills/README.md)。没有启动真实模型或靶场实验，实际测试收益仍待用户验证。

## 2026-10-17 新成员部署整理

增加个人配置生成及首次安装入口，移除共享模板中的维护者 IP/固定目录，支持按 SSH 用户解析远端路径、选择兼容 Python/公开包源和创建首次 venv。日常启动入口保留，个人凭据不覆盖。SSH 恢复后，在 Kali 全新独立目录完成首次及重复安装、23 个安装源码哈希和启动器检查，原配置与日常服务启动身份保持，fixture 和测试库已清理。操作见[新成员指南](team-setup.md)，证据与系统前置条件见[本轮记录](acceptance-results/2026-10-17/team-setup/README.md)；不包含虚拟机/系统软件自动安装或真实模型/靶场实验。

## 2026-10-19 profile 原子登记与迁移复验

profile 登记改为数据库唯一键原子幂等，删除进程缓存和锁；补齐独立进程 HTTP 竞争、事务回滚、GET 提交可见性和旧空 hash 判据。实现见 [decision 0008](decisions/0008-coverage-profile-registration.md)。本轮独立审计 24、旧审计 13、主探针 8 通过；集成 378 passed / 1 skipped，单元 331 passed / 2 skipped，Pyright 无错误。

P12 未追加产品代码；单会话与后续批次仍遵守[迁移门禁](plan-migration-rollout.md)，工程回归不自动批准真实批量 apply。真实 canary 的授权、备份与运行证据另存本机验收目录，不将个人会话或目标数据写入共享说明。

## 2026-10-19 完整方案补齐与受控批次

后续完整方案复核补齐普通 Runner owner / lease 拒写门禁和多组冻结 profile 的报告选择，见 decision 0009、0010。全量集成 386 passed / 1 skipped，单元 331 passed / 2 skipped，独立 / 旧审计 / 主探针 24 / 13 / 8 全通过，静态检查和前端构建通过。

用户另行授权完整迁移链路后，对 9 个合格旧会话实际执行 1 / 5 / 3 三批，新增 45 合同、保留 19 个 legacy_done，不恢复真实测试；逐项验收及停批对账过程见[批次记录](acceptance-results/2026-10-19/plan-rollout.md)。静态条件不足的其他会话继续保留，不自动迁移。

桌面原入口 API-only 烟测、实际 HTTP / 静态资产核对、Kali 离线部署重启与 23 个源码哈希核对均通过，Linux 隔离专项 54 passed。凭据、真实模型调用数和暂停状态保留，未发起真实模型 / 目标测试。部署范围与限制见[双端验收](acceptance-results/2026-10-19/deployment.md)。

## 2026-10-19 外部 Skill 边界恢复

此前外部 Skill 修改留在 stash，当前分支仍含旧 builtin 发布和默认注入路径；已选择性恢复边界并补齐 GET/刷新防回归。空库存恢复原工具和官方循环，历史转换项只保留审计用途。全量 724 passed / 3 skipped，实际列表及显式刷新均为 0，46 张受保护表、凭据和暂停状态保持。最新边界见 [decision 0011](decisions/0011-external-skills-boundary.md)，证据见[本次验收](acceptance-results/2026-10-19/external-skills.md)。

## 2026-10-20 checkpoint 同通道多任务写入修复

最新 Phineas 的收据唯一键冲突来自两个合法 Send 共用通道名。新收据改用官方写入位置，绑定类型和通道哈希；内层写入后核对真实保存内容，恢复按位置过滤，旧收据只在无歧义时兼容。无需新增 migration，保留原 fencing 与历史数据。行为见 [decision 0012](decisions/0012-checkpoint-write-slots.md)，测试及部署证据见[验收](acceptance-results/2026-10-20/checkpoint-writes.md)。

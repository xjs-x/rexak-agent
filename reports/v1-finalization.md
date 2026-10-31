# Rexak Agent v1.0 工程收尾

后续公开验收与交付状态见 [v1.0 公开验收摘要](../docs/validation-v1.md)。本文保留工程收尾时点的结果；下文“日常实例未切换”等描述不代表后续部署状态。

日期：2026-10-30。以本轮工作区代码与可复现证据为准，不用 Git HEAD 代替运行版本。

## 1. 六阶段状态

| 阶段 | 状态与证据边界 |
|---|---|
| 双图前端 | 已完成真实 API、手动刷新、owner 隔离、失败与空态处理；隔离 PostgreSQL + 实际浏览器验证完成 |
| 删除旧模式 | 已删除正式 passive/Shadow/Progress 入口、专用配置与专用测试；Decision Memory 保留 |
| 代码审查 | 已修复明确的图查询 owner 边界、资产 writer/schema 不一致、可选 SDK 静态依赖问题；P2 见下文 |
| 回归与端到端 | 正式最终回归结果见第 5 节；前端 66 项通过；真实模型/CTF 不运行 |
| 部署与切换 | 新库、增量迁移、迁移失败回滚、原桌面启动链的隔离 API-only 均已验证；日常实例未迁移/未上线 |
| 项目文档 | README、部署、当前架构、实现状态和 decision 0018 已更新；历史研究记录保留并标注范围 |

## 2. 双图真实 API 与浏览器

复用 `GET /api/sessions/{session_id}/graph`，增加 owner 检查；会话 Cookie/访问令牌鉴权保持，GET 不要求 CSRF。
读取直接调用 `read_graph`；不写图、推进 revision、确认页或产生回执。关闭攻击图时响应可省略 attack_graph，前端可读取信息图并禁用攻击图标签。

前端支持信息图/攻击图/概览、缩放平移与适应画布、节点与连线详情、来源引用和版本。
新增「刷新图谱」；切换 Session 清理旧焦点和选择。真实会话 404/501/其它错误不再降级样例。
未选 Session 的演示仍明确标注；两图为空显示空态，不推测缺失事实。没有自动轮询。

真实浏览器使用隔离库中的持久化夹具数据，不替换 fetch、不拦截 HTTP。
在 `127.0.0.1:8771` 通过原 `start-workbench.cmd --api-only` 启动链验证，
信息图实际显示 2 个实体/1 条关系，攻击图显示 1 个状态；页面标注「会话数据」。
隔离数据库 revision 从 3 改为 4 后，点击刷新实际显示 v4。
这证明前端接入真实后端，不是 CTF 或模型维护语义正确性的效果实验。

- [信息图截图（隔离验收 Demo）](../docs/assets/readme/information-graph.jpg)
- [攻击图刷新截图（隔离验收 Demo）](../docs/assets/readme/attack-state-graph.jpg)
- HTTP 访问记录：`data/tmp/v1-finalization/isolated-runtime/logs/launcher/api.log`

## 3. 废弃模式与保留机制

正式历史辅助只保留 memory/off；新环境默认 memory，已有 .env 未改写。
原有 off、force_off、Run 固定快照、预算、控制信号及工具消息配对边界保留。
旧 Run 缺模式保持 off；旧 passive 快照映射到 memory，不复活检测器。
LegacyProtocolState 的旧字段仅供历史读取；不保留 Progress 节点恢复桥。处于已退役节点的旧任务不自动恢复。
历史配置与公开消息可读取，不需要运行旧模式。新配置不能选择 passive。

被移除文件：

- `src/agent_workbench/contracts/history_action_reflection.py`
- `src/agent_workbench/domain/history_action_reflection.py`
- `src/agent_workbench/domain/passive_reflection.py`
- `src/agent_workbench/domain/shadow_authorization.py`
- `src/agent_workbench/domain/trusted_http_shadow.py`
- `src/agent_workbench/tools/history_action_shadow.py`
- `src/agent_workbench/tools/history_feedback.py`
- `src/agent_workbench/tools/shadow_storage.py`
- `src/agent_workbench/tools/shadow_test_profile.py`
- `src/agent_workbench/agent/middleware/progress.py`
- `tests/experiments/test_autonomous_shadow.py`
- `tests/experiments/test_autonomous_shadow_formal.py`
- `tests/experiments/test_history_action_shadow.py`
- `tests/experiments/test_pilot_readiness.py`
- `tests/experiments/test_reflection_preparation.py`
- `tests/experiments/test_trusted_shadow_boundaries.py`
- `tests/experiments/test_trusted_shadow_integration.py`
- `tests/integration/test_autonomous_shadow.py`
- `tests/integration/test_autonomous_shadow_evidence.py`
- `tests/integration/test_progress.py`
- `tests/integration/test_reflection_preparation.py`
- `tests/unit/test_history_action_reflection.py`

清理专用 `history_action_*` 设置、配方工具 `run_http_test_profile`、HTTP dispatch 观察 hook、Runner shadow 关闭 hook、Toolbox shadow/可见投影字段及 Progress 装配。
混合测试中保留 memory 证据、预算、控制信号与来源隔离断言；共享 Operation 测试夹具迁到实际使用者，未删除执行可靠性测试。

保留 `(run_id, tool_call_id)`、OperationRegistry、参数冲突拒绝、结果复用、Executor Ledger、租约、控制 fence、未知状态对账、Checkpoint 恢复及原始 Artifact。
Decision Memory 的检索、WorkingContext、ContextBuilder、来源记录、历史参考可见性去重仍保留。
`_host_history_reference` 是现有 memory 低权限投影容器，不是已删除检测器的运行入口。
正式 `src/` 和 `scripts/` 无检测器导入或可执行入口；历史实验源码/结果仅为归档，不改写冻结内容。

## 4. 审查与修复

审查实际 create_agent 装配、请求包装、ToolDispatch、Operation 身份、Runner 收尾、Session 图、鉴权、附件/CAS、二进制静态工作进程、数据库约束与前端来源路径。
这是有界工程审查，不是完整安全证明。

| 等级 | 明确问题 | 实际修复与验证 |
|---|---|---|
| P0 | 图查询只验证本地登录，未核对 Session owner | 增加 owner 条件并统一对不存在/非本人返回 404；保留脱敏和只读性测试 |
| P1 | 资产 writer 依赖 `uq_assets_endpoint_identity`，迁移却删除它 | 新增 f19a30b742c1，撤销不匹配的 canonical 部分索引，恢复 method+identity 唯一约束；不合并或删除任何历史行 |
| P1 | 可选 TSec SDK 的直接导入使干净 dev 安装的类型检查依赖未选 extra | 改为按需 importlib 加载，缺依赖仍显式 PlatformUnavailable；不增加基础依赖 |
| P1 | 真实 Session 接口失败时前端可能显示虚构样例 | 失败显示错误与可重试入口；只有明确未选择 Session 时保留演示标记 |

资产修复不扩工具权限、不改变图算法。旧 Alembic head 断言更新为本轮真实 head，仍测试旧/未来版本拒绝。

P2/已知限制：历史实验导入器假设旧列、工具请求断言落后、冻结实验源码与当前版本不同；全仓 Ruff 的归档实验问题；窄视口图谱详情可能横向溢出；较大前端 chunk 警告。
没有重写 Planner/Decision Memory/Executor，也没有为了全绿删掉正式断言或修改历史试验数据。

## 5. 验证命令与结果

最终正式结果：`uv run pytest tests/unit tests/integration -q` → **1019 passed、3 skipped、0 failed，509.58 秒**。
跳过为 2 项 Windows 符号链接权限和 1 项未配置真实模型凭据。

| 检查 | 结果 |
|---|---|
| `uv sync --frozen --all-packages --group dev` | 通过；基础安装不要求 benchmark extra |
| `uv run pyright` | 0 errors / 0 warnings |
| `uv run ruff check src executor/src tests scripts evals` | 通过 |
| `uv run ruff check .` | 归档 experiments 范围 43 项问题，未改冻结源码掩盖失败 |
| `uv run python scripts/check_encoding.py` | 660 个文件 UTF-8 无 BOM 检查通过 |
| `pnpm --dir web install --frozen-lockfile` | 通过 |
| `pnpm --dir web exec vitest run` | 66 passed，6 个测试文件 |
| `pnpm --dir web build` | TypeScript/Vite 通过，保留较大 chunk 警告 |
| `uv run pytest tests/experiments -q` | 120 passed、90 failed、6 skipped；历史研究套件，不混入正式通过数 |

第一次正式执行在收集时暴露被删 Progress 测试文件提供的共享夹具依赖，已移到实际使用者；
首个完整回归 1016 passed、2 failed、3 skipped，2 个本轮测试导入遗漏已修复。
最终完整回归用于验证修复，不将初轮失败伪称通过。删除的测试仅针对退役模式，测试总数不能与原版本直接横比。

本地原始证据在 `data/tmp/v1-finalization/`：pytest-first/final/release、web-release、pyright-release、ruff-all、migration-*、experimental-tests 等日志。
新增报告不保存凭据、Cookie、隐藏推理、目标答案或原始敏感正文。

## 6. 跨模块协议验收

复用 `test_minimal_autonomous_loop.py` 的 API → Runner → 官方 Agent → 稳定 Operation → 工件 → 独立 Saver 重读 → 最终结果归档；
复用 execution_delivery_recovery、tool_dispatch、persistence、natural_completion_safety 验证 Job、控制、消息交付和恢复不重放。
`test_session_graph.py` 验证真实 PostgreSQL 图写、来源、回执、五轮同步、Checkpoint 重连与压缩；
`test_session_graph_read_api.py` 验证读取、脱敏、空图、owner、已删除会话以及读前后图/同步字段/事件/回执不变。
Decision Memory 现存单元与集成断言覆盖来源可见性、未知状态与身份变化、低权限请求投影、预算及供应商错误不重调。
附件、二进制分析、资源取消与权限测试属于同轮正式回归。

这些是互补的跨模块确定性协议路径，不声称一次真实目标运行已经端到端通过所有阶段。
没有真实 CTF、Flag 提交、付费模型请求；没有向日常库注入验收任务。

## 7. 部署、迁移与数据保护

隔离容器 `rexak-v1-finalization-db` 仅映射本机 55439，独立于日常 workbench-postgres 的 55432。
测试夹具在独立随机测试库中执行；隔离 API 使用专用 DATA_ROOT、8771 端口，且仅 API-only。
Docker Desktop 启动后已有日常 PostgreSQL 依其策略启动；未启动日常 API/Runner，未对日常库执行迁移或业务写入。

已验证：新库 db-init、e82d934a10bc → f19a30b742c1 增量升级、旧资产身份与内容保留。
还在隔离库注入一个重复资产键：升级明确失败且 schema/head/两条记录均回滚保留；仅删除本轮那个重复夹具后升级成功。
这不是对日常记录的修复或删除。所有维护删除只针对明确创建的隔离夹具。

日常检查：head=e82d934a10bc，有效 Run 租约 0、queued=2044、非终态 Job=831、uncertain Job=832（可重叠）。
重复资产键组为 0，但历史队列和未知执行状态尚未解决，因此不强制上线。
Kali installed 文件 23 项哈希与本机一致，身份/能力正常，active_jobs=0；本轮未改执行端源码，不重新部署或重启。

原桌面快捷方式仍使用 start-workbench.cmd，隔离参数启动链已验收。API/Runner 要重启才加载新代码，
但日常 API/Runner 未启动；正式版本尚未切换。临时 API 在烟测结束后关闭。

## 8. 文档、发布结论与后续步骤

已整理 README、docs/architecture.md、docs/deployment-quickstart.md、docs/implementation-status.md 和 decision 0018。
旧设计/进度文档标注历史范围；六条 CTF 复盘保持候选/应用通关/平台验证三种证据等级，不重新运行实验。

当前是工程收尾候选，不能无条件宣称正式 v1.0 已发布或日常已上线。
发布阻塞：维护方需先解决历史队列、未决 Job 及退役节点任务的处置；全仓检查和历史研究兼容缺口仍公开保留。
正常正式路径的最终测试是否通过见第 5 节；当前未发现额外已证实而未处理的生产 P0/P1，但不把未验证范围说成无缺陷。

维护窗口按部署文档：停止新提交，核对/暂停与对账，备份数据库和工件，迁移，先 API-only 读取历史与图，再定向恢复已明确授权的任务。
不要清队列、改旧状态、启动无过滤 Runner 或删除数据卷。无需用户更新 Kali；只有未来确实改执行端时才需要现有部署流程。
真实模型与靶场效果测试仍由用户执行，本轮不代为启动。


最终保全核对：daily-before/daily-after 的业务表计数、全部非终态 Run 状态摘要及 .env 哈希一致；
Kali boot_id 未变。日常 Alembic head 仍为 e82d934a10bc，未代用户执行正式迁移。
最终跨模块协议测试、资产并发、状态版本和图 owner 用例均包含在 1019 项正式通过中。
本轮差异以工作区开始快照对比记录于 change-inventory.json，保留原有未提交修改；未提交或推送 Git。


### 失败归属说明

- 既有资产并发 3 项：原约束与迁移不一致，本轮已修复并在最终全量通过。
- 历史 Alembic head 固定断言：任务开始代码已经随前次迁移更新；本轮新增迁移后再更新为真实 head，旧/未来版本拒绝断言保留且通过。
- 本轮新增问题：清理时共享测试夹具导入失效及 2 项测试导入遗漏，均已修复并由最终完整回归验证。
- 环境跳过：符号链接权限 2 项、缺真实模型凭据 1 项，不记为成功。
- 额外历史研究套件 90 项失败未逐项做开发前对照，不能一概称为既有或本轮新增；
  冻结源码一致性校验会因本轮授权修改失败，旧字段导入和旧请求合同断言也有兼容缺口。
  原始报告/冻结不更新，不将这 90 项隐藏或算成通过。若发布要求 tests 全目录全绿，仍有明确阻塞。

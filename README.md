# Agent Workbench

个人使用的 **Web / API 自主黑盒测试工作台**。

用户创建项目与任务，提供测试范围与可选测试账号；Agent 自主探索目标、维护假设、
选择工具、验证线索并持续产出带证据的结论。在目标达成、探索收敛或需要用户补充条件时
结束本阶段，后续输入继续沿用同一任务记录。

本仓库是独立新项目，**不 import 旧 `xak` 包、不依赖旧目录存在、不继承旧数据库结构**。
编码方案与验收口径见 `../xak-agent-rebuild-plan/`。

---

## 1. 三个进程

| 进程 | 运行位置 | 职责 | 禁止承担 |
|---|---|---|---|
| **API** | 控制端（本机 Windows） | 指令持久化、查询、SSE、配置、工件授权读取 | 在请求处理函数里运行整段 Agent |
| **Runner** | 控制端 | 驱动 Agent、读指令、调模型、调度工具、恢复 | 直接在控制机执行模型生成的命令 |
| **Executor** | 独立 Linux/Kali 执行岛 | 浏览器、HTTP 会话、命令、后台任务、执行输出 | 持有模型 API Key 或控制端数据库凭据 |

**运行与浏览器连接解耦**：关闭网页、SSE 断开都不会停止任务。

```
UI(React) ──REST/SSE──> API ──> PostgreSQL
                         ▲
Runner ──模型──> 小米/后续供应商
   └──SSH 隧道──> Executor(执行岛) ──> 目标网站/API
                      └──> 岛上工件与执行台账
```

---

## 2. 环境要求

快速部署步骤见 [简易部署指南](docs/deployment-quickstart.md)；Executor 的配置边界与浏览器登录细节见 [P3–P5 部署说明](docs/p3-p5-deployment.md)。

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **3.12.x** | `.python-version` 锁定 `3.12`；由 uv 管理 |
| uv | ≥ 0.9 | 依赖与虚拟环境管理（本机安装在 `~/.workbuddy/bin/uv/uv.exe`） |
| Node.js | 22 LTS | 前端构建 |
| pnpm | ≥ 10 | 前端包管理 |
| Docker + Compose | ≥ 27 | 仅用于本地 PostgreSQL |
| SSH 客户端 | OpenSSH | 连接执行岛 |

---

## 3. 干净安装与启动

```powershell
# 1) 依赖（锁定版本，禁止未锁定的 latest）
uv sync --frozen --all-packages --group dev

# 2) 配置
Copy-Item .env.example .env
#    编辑 .env：数据库、控制端令牌、执行岛地址与 SSH 密钥路径
#    模型和 API Key 在启动后的 Web 模型设置中配置并保存，不写入仓库。

# 3) 启动 PostgreSQL（首次）
docker compose up -d postgres

# 4) 初始化数据库：框架表（checkpoint/store）+ 业务表（Alembic）
uv run workbench db-init

# 5) 自检：进程、数据库、模型、执行岛
uv run workbench doctor --skip-model
uv run workbench doctor --skip-model --target-url http://127.0.0.1:8899/ --scope-file deploy/scope-local-fixture.json
```

第二条命令从执行岛探测 DNS 与 HTTP；此处的 `127.0.0.1` 是执行岛本机。
目标映射、登录状态与已验证能力边界见 [P3–P5 部署说明](docs/p3-p5-deployment.md)。

日常使用保留唯一启动入口：双击桌面 **Rexak Agent**，或在本项目目录运行：

```powershell
.\start-workbench.cmd
```

启动器检查数据库和 Kali Executor，启动 API、Runner 并打开控制台，不再在终端要求选择模型或输入 Key。
在左侧“模型设置”填写供应商 Base URL 与 API Key，保存后下次启动仍可用。输入框上方可选择小米三款模型或百炼 `deepseek-v4.1-flash`。
Windows Key 用当前用户 DPAPI 加密保存；同一供应商共用 Key，接口不返回原值。运行阶段固定模型与接入地址；新聊天或新阶段可切换，暂停继续保留原模型。
官方额度、旧任务预算升级和验收边界见[模型配置与预算说明](docs/model-settings.md)。

以下命令仅供维护和调试，日常无需分别启动：

```powershell
uv run workbench api
uv run workbench runner
uv run workbench-executor   # 仅在 Kali 执行岛运行
```

前端开发：

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev                    # Vite dev server，代理 /api 到 127.0.0.1:8770
pnpm build                  # 构建后由控制端在 /console/ 提供
```

启动 `uv run workbench api` 后，访问 <http://127.0.0.1:8770/console/> 使用基础控制台。
它已提供项目与任务、对话输入、运行控制、发现证据、后台任务、费用、控制端耗时诊断与报告版本。
旧的只读诊断页仍在 <http://127.0.0.1:8770/observer>。实时活动可逐页浏览，长日志只渲染可见行，点击事件从脱敏接口读取详情。“对话归档”按页显示用户指令和模型文本，并可分段展开长正文；模型正文从此版本开始归档，旧调用不会自动回填，也不作为 Agent 记忆。“当前计划”直接读取 LangGraph checkpoint，关系图只展示步骤明确关联的线索。耗时诊断把 Executor 接收、开始、结束时间分别用于排队和执行区间；缺少时间戳时显示未知。模型首个可见分片仅在流分片与最终消息 ID 匹配时记录，不能当作供应商首字节耗时。结束提议检查、Runner 复核和报告生成分别计时；报告进一步区分快照采集、证据校验、正文渲染和版本发布，并在失败时标记中断阶段。Git HEAD 独立核验区分预检查、复测调用返回和结论写入；后台复测的实际运行时间仍以任务台账为准。设置页可录入每百万 token 的模型价格，生成不可变价格版本；之后启动的运行使用新版本，历史费用保留原版本。真实小米/Kali 任务轨迹尚未验收。

后端接口变更后，重新生成前端契约类型：

```powershell
uv run python scripts/export_openapi.py web/openapi.json
cd web
pnpm generate:api
```

报告可通过 `POST /api/sessions/{session_id}/reports` 创建版本，随后用
`GET /api/sessions/{session_id}/reports` 查询历史版本，并用
`GET /api/sessions/{session_id}/reports/{version}/export?format=markdown|html`
下载附件。写请求需要本地会话 Cookie 与 CSRF 头。报告只将带独立验证记录且证据工件
哈希可核对的发现列为已确认；候选发现单列，旧版本保留。当前只接入一类窄范围的
确定性核验工具：`verify_public_git_head` 对 `/.git/HEAD` 做隔离 HTTP 复测，
仅能确认 Git HEAD 分支引用公开可读，不能推断整个仓库或源码可读。

领域记录可通过 `GET /api/sessions/{session_id}/assets`、`/leads`、`/findings`、
`/coverage` 查询；单条发现的验证历史通过
`GET /api/sessions/{session_id}/findings/{finding_id}/verifications` 查询。
资产、线索和验证历史支持 `limit`、`offset` 分页；线索还可按 `status` 筛选。

> 默认 compose 重建应用**不删除**数据库与工件卷。
> **禁止**把 `docker compose down -v` 当作日常启动步骤，那是破坏性操作。

---

## 4. 标准验证命令

根目录：

```powershell
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration -q
uv run pytest tests/recovery -q
```

前端 `web/`：

```powershell
pnpm lint; pnpm typecheck; pnpm test; pnpm build; pnpm test:e2e
```

真实基础设施与模型：

```powershell
uv run workbench doctor --skip-model
uv run python evals/runner.py --suite smoke --provider xiaomi
uv run python evals/runner.py --suite acceptance --provider xiaomi --trials 3
```

只能在 Linux 执行岛验证的部分（进程控制 / 软链接逃逸 / 工作区文件实机链路）：

```bash
uv run python scripts/executor_process_check.py
```

进程级重启演练（需要真实模型与可达执行岛）：

```
XIAOMI_API_KEY=<本机密钥> uv run python scripts/restart_drill.py
```

调试时可以只推进指定 run（避免被数据库里其它 queued run 拖住）：

```powershell
uv run workbench runner --once --only-run-id <run_id> --no-recover
```

**无凭据时相关测试标记 `NOT_RUN`，不能算通过。模拟通过不代表真实模型通过。**

---

## 5. 目录导览

```text
src/agent_workbench/
  api/            FastAPI 路由、鉴权、SSE
  contracts/      协议真源（请求/结果/事件模型）——前端类型由此生成
  config/         设置、模型目录、价格目录、密钥存储
  domain/         任务、资产、线索、发现、验证规则
  agent/          create_agent 装配、图状态、middleware、提示
  runtime/        Runner 运行所有权、指令消费、恢复、阶段结束
  models/         供应商适配、能力探测、用量
  memory/         任务笔记、上下文构造、Store 访问
  tools/          Agent 可见工具及权限/并发包装
  execution/      Executor 客户端、SSH 隧道、远端事件同步
  evidence/       工件存储、索引、脱敏视图、验证记录
  persistence/    ... 业务表、仓储、事务
  observability/  事件、日志、span、费用计算
  reporting/      报告快照与渲染
executor/src/workbench_executor/   执行岛常驻服务
migrations/      Alembic，**仅业务 schema**
tests/           unit / integration / recovery / e2e
evals/           场景、fixture、grader、runner
docs/            decisions / implementation-status / acceptance-results
```

运行数据写在 `WORKBENCH_DATA_ROOT`，**不放进源码包**。

---

## 6. 关键设计约束（改代码前必读）

1. 基础模型—工具循环使用官方 `create_agent`，**不写第二套自研 ReAct 主循环**。
2. 每项信息只有一个权威归属：图状态 → LangGraph checkpoint；领域记录 → 业务表；
   真实执行状态 → Executor 台账。
3. 模型猜测**不能**直接写成已确认事实；`confirmed` 必须有验证记录。
4. 不设任务总时长/费用硬上限；单次操作仍有超时、取消与资源边界。
5. 持久操作身份：重放复用 `operation_id`；显式重测生成新的逻辑操作 ID。
6. 全部源码/配置/日志 UTF-8 无 BOM；不做静默 GBK/Latin-1 兜底。
7. 长正文不塞 checkpoint / events / findings，落工件后只传摘要与引用。

详细状态见 `docs/implementation-status.md`；已锁定决策见 `docs/decisions/`。

V1 六阶段代码已接通。验收证据及限制见 [V1 实施进度](docs/implementation-v1-progress.md)，启用、独立试验、封包和离线复盘见 [操作说明](docs/experiments/v1-operation-guide.md)。工程回归不代表真实靶场效果验收。

---

## 7. 当前状态

见 **`docs/implementation-status.md`**（P0–P9 逐项状态 + 证据）。只有具备验证证据的条目才标完成。

已完成并通过验证：**P0、P1、P2**；P3–P5 已有可运行切片，Kali 执行岛关键链路已验证，真实模型验收仍有缺口。P6–P8 部分落地。

当前能力边界（未验证的部分不得声称"已支持"）：

| 能力 | 现状 |
|---|---|
| 前端控制台（P7） | 基础控制台已可构建；事件历史虚拟列表与新产生的对话正文归档已接入，真实模型长任务交互和细粒度阶段时间线仍待验收 |
| 浏览器 / 登录探针（P4） | 本机 Edge 与 Kali Chromium 已验证；真实模型任务未复测 |
| 报告与独立复测（P6） | 版本创建和 Markdown/HTML 导出已通过集成测试；Git HEAD 引用复测工具已接入，真实模型及执行岛端到端效果尚未验收，其他漏洞类型没有自动核验器 |
| 文件 read/write/edit 工具（P3） | 已实现；Linux 路径与软链接专项测试已在执行岛通过 |
| 通用命令与持久执行资源 | 指定 Kali 显式 trusted 时启用；默认发行 disabled，不承诺系统级出站隔离。见 [使用说明](docs/execution-runtime.md) |
| 效果 grader / 配对基线（P9） | 八个自然目标、独立原件判定与离线复核已实现；真实重复 trial 与旧 B1 配对效果未验收 |

已锁定的技术决策与实测结论见 `docs/decisions/`。

# P8 隔离备份还原演练

本机使用 `compose.yaml` 的 PostgreSQL 容器时，备份命令可通过
`workbench backup --docker-container workbench-postgres` 在容器内执行 `pg_dump`。
`--docker-container` 只接受配置为本机回环地址的数据库；运行前请确认该容器对应
`WORKBENCH_DATABASE_URL`。不指定时使用 PATH 中的本机 `pg_dump`。
备份完成后运行 `workbench verify-backup <备份目录>` 可核对 SQL 和工件的
SHA-256 清单；该校验用于发现意外损坏，不提供防篡改认证。

运行 `python scripts/backup_restore_drill.py` 会创建两份带 `p8_drill_` 前缀的
临时数据库，调用实际备份命令，再将 SQL 导入另一份数据库，核对 UTF-8 记录与
工件正文，结束时删除这两份临时数据库。它不会还原或覆盖工作库。

升级代码后先备份并运行 `workbench db-init`。API 和 Runner 启动时只读核对业务库
Alembic 版本与当前代码的迁移 head；不一致会拒绝启动，不会自动改表。
Runner 恢复已有 LangGraph checkpoint 前也会核对状态版本；缺失或不匹配时拒绝续跑，
以免把旧状态误当作新任务并重复执行工具。当前只有状态版本 v1，跨版本转换尚未实现。

事件流按 `WORKBENCH_EVENT_RETENTION_DAYS`（默认 30 天）提供**手动**保留策略；
`workbench prune-events` 只预览，`workbench prune-events --apply` 才按批删除。
可加 `--session-id <任务 ID>` 限定任务、`--limit 1000` 控制单批数量。
按数据库写入时间判断过期；有活动 run 的任务完全跳过，每个任务至少保留最新事件。
清理后的旧浏览器游标会收到重新获取快照的提示。该命令不删除报告版本、证据工件、
模型调用台账或其他业务记录；需要完整审计轨迹时，应在清理前先做数据库和工件备份。

运行中显式修改范围使用 `PUT /api/sessions/{session_id}/scope`，请求体需包含
`expected_revision`、新 `scope`（其 `revision` 为旧值加一）与非空 `reason`，
并像其他写接口一样携带本地会话、CSRF 与 `Idempotency-Key`。更新会让旧提议停止
派发；已经提交的 job 继续核对真实状态，并作为历史结果展示，不会被盲目重做或
直接纳入新方向的结论。当前控制台只展示范围，编辑入口仍需单独接入。


## 隔离回归与运行诊断

运行数据库集成或恢复测试时，pytest 自动创建随机 `workbench_test_*` 临时库和工件目录，初始化后再运行用例，结束时清理。本地测试数据库用户需有创建数据库权限；测试不再扫描日常工作库。初始化或清理失败会明确报错，不能记为通过。只运行单测时无需真实数据库。

`workbench export-run <run-id>` 输出诊断 v2 和 SHA-256 清单，按指定 run 收集保留事件、模型调用、操作、远端任务、观察和工件完整性。领域知识单独标为导出时的当前会话快照，不冒充历史 run 成果；工件哈希在数据库快照之后检查。按已知规则脱敏，不含工件正文、完整模型请求或 checkpoint，也不能保证识别自由文本中的未知凭据。

模型连续无有效分片默认等待 300 秒（`WORKBENCH_MODEL_REQUEST_TIMEOUT_SECONDS`），持续返回正文、思考或工具参数时会延长等待；单次调用另有 1800 秒墙钟保护（`WORKBENCH_MODEL_REQUEST_MAX_SECONDS`）。小米的 `reasoning_content` 会保留并在后续工具轮次回传，界面只显示思考状态，不展示思考原文。三条数据库连接路径默认 10 秒（`WORKBENCH_DB_CONNECT_TIMEOUT_SECONDS`）。超时不自动重发模型请求；点击继续或提交新指令可以恢复，存在未核实工具任务时仍需明确核对。不构成任务总时长或费用上限，费用未知或未定价时仍保持未知。

评测入口保留 smoke，并已接入八个自然目标的 acceptance 矩阵、独立靶场重置、原件判定和离线复核。运行与交接方式见 [P9 评测说明](docs/p9-evaluation.md)。真实模型重复 trial 与旧 B1 配对结果尚未取得，不能用机制测试替代效果验收。真实 Earth 任务状态及本轮修复证据见 [验收记录](docs/acceptance-results/2026-10-10/README.md)。

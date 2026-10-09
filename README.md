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
#    密钥不要写进 .env 提交；使用 XIAOMI_API_KEY 环境变量或 WORKBENCH_SECRET_STORE_PATH 指向的文件

# 3) 启动 PostgreSQL（首次）
docker compose up -d postgres

# 4) 初始化数据库：框架表（checkpoint/store）+ 业务表（Alembic）
uv run workbench db-init

# 5) 自检：进程、数据库、模型、执行岛
uv run workbench doctor
```

启动服务（三个终端，或各用 `-d`）：

```powershell
uv run workbench api        # 控制端 API + SSE，默认 127.0.0.1:8770
uv run workbench runner     # Agent Runner；需要有效 run lease 才会推进任务
uv run workbench-executor   # 在**执行岛**上运行，只监听回环地址
```

前端开发：

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev                    # Vite dev server，代理 /api 到 127.0.0.1:8770
```

> 前端控制台属 **P7，尚未建立**。当前可用的只读观察窗口由 API 直接提供：
> 启动 `uv run workbench api` 后访问 <http://127.0.0.1:8770/observer>。
> 它显示快照、SSE 事件流、助手输出与用量；不提供项目管理与黑板图。

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
uv run workbench doctor
uv run python evals/runner.py --suite smoke --provider xiaomi
uv run python evals/runner.py --suite acceptance --provider xiaomi --trials 3
```

进程级重启演练（需要真实模型与可达执行岛）：

```powershell
$env:XIAOMI_API_KEY = "<本机密钥>"
uv run python scripts/restart_drill.py
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

---

## 7. 当前状态

见 **`docs/implementation-status.md`**（P0–P9 逐项状态 + 证据）。只有具备验证证据的条目才标完成。

已完成并通过验证：**P0、P1、P2**；P3/P5/P6/P8 部分落地。

明确**未做**（对外不得声称"已支持"）：

| 能力 | 现状 |
|---|---|
| 前端控制台（P7） | 未建立；仅有 `/observer` 只读窗口 |
| 浏览器 / 登录探针（P4） | Executor 对 `/browser-sessions` 返回 **501** |
| 报告生成（P6） | `POST /api/sessions/{id}/reports` 返回 **501** |
| 文件 read/write/edit 工具（P3） | 未实现 |
| 通用命令无人值守模式 | **已关闭**：执行岛无执行环境级出站约束 |
| 效果 grader / 配对基线（P9） | 未实现；`evals/runner.py` 只报告观察 |

已锁定的技术决策与实测结论见 `docs/decisions/`。

# rexak-agent · 项目长期约定

> 作用域：`C:/xjs/rexak-agent`。跨会话、跨阶段仍有效的规则与事实。

## 项目定位

Web/API 自主黑盒测试工作台。设计基线在 `C:/xjs/xak-agent-rebuild-plan/`
（BUILD_PLAN / IMPLEMENTATION_CHECKLIST / ACCEPTANCE / LEARNING_PLAN）。
本仓库**不 import 旧 `xak` 包、不依赖旧目录、不继承旧库结构**。

## 技术基线与锁定

- Python **3.12**（`.python-version`），实际 3.12.14，由 uv 管理。
  `requires-python` 放宽到 `>=3.12,<3.14` 只为执行岛（Kali 只有 3.13），
  理由见 `docs/decisions/0002`。
- 一个 `uv.lock`（91 包）。CLI：`workbench` / `workbench-executor`。
- PostgreSQL 16.4 于 `127.0.0.1:55432`（compose service `postgres`）。
  三个 schema：`app`（Alembic 管）、`agent_checkpoint`、`agent_memory`（框架 setup 管）。

## 环境事实（本机）

- uv 二进制：`~/.workbuddy/bin/uv/uv.exe`（不在 PATH 里，需全路径）。
- 执行岛：`192.168.72.134`，用户 `kali`，密钥 `~/.xak/id_ed25519`。
  Executor 装于 `~/rexak-agent-executor`，起停用 `./start.sh` / `./stop.sh`；
  测试目标用 `./start-target.sh`，监听 `127.0.0.1:8899`。
  **岛上的 `~/.xak/id_ed25519` 与 executor.env 不入库。**
- 小米凭据在 `~/.xak/llm-keys.json`；**永远不要**把真实密钥写进仓库。
  运行测试/评测时用 `XIAOMI_API_KEY=$(...) python -c ...` 临时注入。
- 执行岛 GitHub 不通，PyPI 通（用 `https://pypi.tuna.tsinghua.edu.cn/simple`）。
- Kali 登录 shell 是 **zsh**：`pkill -f workbench_executor` 会匹配到 SSH 命令自身
  并杀掉会话 —— 一律用 PID 文件管理进程。

## 硬性工程约定

1. 用官方 `create_agent`，**不写第二套主循环**。
2. 权威归属唯一：图状态→checkpoint；领域记录→业务表；真实执行状态→Executor 台账。
3. 模型只能写 `candidate`；`confirmed` 必须有验证记录（有 DB CHECK 兜底）。
4. 重放复用 `operation_id`；工具函数内**禁止**随机生成 `operation_id`。
5. 长正文落工件，checkpoint/events/findings 只放摘要与引用。
6. 全部文件 UTF-8 无 BOM；**不做** GBK/Latin-1 兜底猜测。
7. 不吞异常；工具异常在 `ToolDispatchMiddleware` 分类成结构化结果。
8. 未验证的能力在代码/文档里必须显式标未实现（如浏览器 501、报告 501）。

## 易踩的坑（都是真实踩过的）

- `alembic.ini` 必须纯 ASCII（Alembic 用 locale 编码读它，中文会直接崩）。
- Windows 上 psycopg 异步必须用 `SelectorEventLoop`：
  入口统一调 `platform_compat.configure_event_loop_policy()`。
  **注意 uvicorn 的 `Server.run()` 会把策略改回 Proactor** —— API 必须程序化启动
  （`asyncio.run(server.serve())`），不能直接 `uvicorn.run(...)`。
- **子进程必须固定 UTF-8**：用 `platform_compat.child_process_env()`。
  否则 cp936 控制台下子进程写 GBK、父进程按 UTF-8 解码，异常会被 `subprocess`
  在读线程里吞掉，表现为莫名其妙的 `proc.stdout is None`。
- **`uv run pytest` ≠ `python -m pytest`**：console script 不把 CWD 放进 `sys.path`，
  所以 `from tests...` 只在后者可用。验收命令是 `uv run pytest`，别只用后者自测。
- `EventType` 在 `contracts.events`，**不在** `contracts.enums`（有 AST 回归测试）。
  另外 `EventRow.type` 是 str，和枚举比较要用 `str(...) == str(...)`，不能用 `is`。
- `AgentMiddleware.name` 是 property，子类**不能**用类属性覆盖。
- `SummarizationMiddleware` 的 `fraction` 触发要求模型 profile；
  用 `ProviderProfile.input_budget_tokens` 算绝对 token 数传给 `("tokens", N)`。
- `tools/__init__.py` 保持不导出，避免
  `agent.context → tools.runtime → tools/__init__ → tools.http → agent.context` 循环。
- 工件内容寻址：写测试时相同内容会命中同一 sha256 行，用唯一内容避免误判。
- **控制版本检查要比较"两个不同时刻"的值**：一个在模型请求前固定、一个在派发前读。
  拿当前值比当前值等于没检查。
- **恢复必须先核对远端、再决定是否续跑**；"没查到"不等于"没有副作用"。
- **状态迁移要走 `set_run_status`**，不要直接改 `run.status`；否则
  `ALLOWED_TRANSITIONS` 就不是真源（`recovering -> queued` 曾因此漏掉）。
- 改代码不要用"按行号切片替换"的脚本 —— 曾误伤另一个分支的循环块。
  优先用精确字符串匹配的编辑。

## 真实演练（不是可选）

`scripts/restart_drill.py` 这类"真实进程 + 真实远端 + 真实时序"的验证
抓到了单元测试抓不到的缺陷：本轮 7 个修复里 **4 个**来自演练
（404 崩溃、单 job 异常中断、核对串行过慢、stale 判定口径不符）。

`scripts/executor_process_check.py` 同理：只在 Linux 才能验证的
进程组/`/proc`/软链接语义，一键同步到执行岛跑（岛上 20 passed）。

**写"一键验证脚本"时要特别小心它自己骗你**：已经踩过两次 ——
解包路径写错导致测的是旧安装代码；`cmd | tail` 的退出码取 tail 的，
测试失败也判成功（必须 `set -o pipefail`）。改造脚本后要先故意制造一次失败确认它真的会红。

## 状态机与"确定结论"的语义

- `unknown` **不是**确定结论：既不能证明完成、也不能证明没发生。
  恢复与唤醒都不得据它继续推进；只能由用户显式确认（resume）后处理。
- 所有状态迁移走 `set_run_status`，让 `ALLOWED_TRANSITIONS` 成为唯一真源。
- 投影（`tool_jobs`）照实同步观察到的状态；**决策**另算 —— 两者不要混在一起写。

## V3 弱点关系（已落地，`docs/plans/implementation-v3-weakness-chains.md`）

- **Finding 就是弱点**：复用 `findings` 表，新增 `weakness_kind / applicability /
  provided_conditions / connection_review / content_hash`；**不**另建同步写入的 Weakness 主表。
- 关系表 `lead_weakness_links`：`role = basis | result`；`evidence_linked` 只表示
  **关系**有依据，两个弱点分别成立**不代表**组合成立（组合必须另有验证记录）。
- 条件 key 由领域层派生（`canonical_condition_key`：折叠空白 + casefold）；
  **同内容重复写入不推进 `revision`**，`content_hash` 覆盖实质内容但不含用途判断元数据，
  所以写"已分析后续用途"不会让自己立刻过期。
- 关系/条件写入必须带 `source_weakness_revision`（或什么都不带）：**旧版本来源明确冲突**，
  不自动升级为新版本判断；诚实比"看起来更完整"重要。
- 顺序坑：**先建立关系，再写 `actionability_assessment`**。反了的话依据指纹当场不同，
  判断立刻变 `needs_review`（这是设计，不是 bug）。
- 结束判定由**程序**枚举复查集合（`domain/exploration_review`），
  `blocked/deferred/outside_goal/not_required` 都不再豁免；分页只决定"这一次显示哪几项"，
  每次 evaluate 都会重新全量核对。
- "呈现"只认**真正进入最终 payload** 的条目：`manifest.presented_weakness_ids` 决定
  checkpoint 游标，被预算裁掉的条目不推进游标（`_trim_working` 按整条对象裁剪）。
- `presented_weakness_ids` 只在非空时写入 —— 这样最后一轮"没有新弱点"不会把
  之前真的呈现过的记录清掉。
- 状态 schema 3→4：`plan[].lead_ids/kind` + 呈现/召回进度字段；`adapt_v3` 只做映射与置空，
  **不**回写虚构的关系或步骤类型。`snapshot.tasks` 要用 `getattr(..., None)` 取，测试替身可能没有。

## 常用命令

```bash
# 控制端
uv run workbench db-init
uv run workbench api        # 只读观察器 http://127.0.0.1:8770/observer
uv run workbench runner
uv run workbench doctor
uv run workbench export-run <run_id>

# 验证
uv run ruff check . && uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration -q
uv run pytest tests/recovery -q
uv run python evals/runner.py --suite smoke --trials 1
```

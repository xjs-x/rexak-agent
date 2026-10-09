# 0003 · 运行边界修复：方向变更拦截、恢复顺序、命令可复现性

- 状态：**已修复并验证**
- 日期：2026-10-09
- 关联：BUILD_PLAN §4.3 / §6.4 / §16.3；ACCEPTANCE §2（A05/A07/A13）、§3 切点 2

独立复核指出三处问题，全部属于"实现与设计承诺不符"而非"功能缺失"。
三项都已修复，并补了能真正暴露问题的测试（含一次真实的进程级演练）。

---

## 1. 方向变更无法可靠拦截旧模型响应

### 问题

`ToolDispatchMiddleware` 在工具参数里没有版本信息时，退化成
`int(args.get("__control_version__", run.control_version))` —— 拿**数据库当前值**
去和**数据库当前值**比较，恒等。而 `AgentRunner._assert_dispatch_allowed`
只核对所有权与暂停/取消状态，完全不看控制版本。

后果：用户在模型思考期间改变方向，旧响应里的工具仍会被派发。

### 修复

| 位置 | 做法 |
|---|---|
| `CommandGuardMiddleware.abefore_model` | 在**模型请求发出之前**读取 `run.control_version`，写入图状态 `dispatch_control_version`，并同步到 `Toolbox.control_version` |
| `ToolDispatchMiddleware.awrap_tool_call` | 用状态里的**固定值**与数据库当前值比较；不一致 -> 该 tool_call **不执行**，返回合法终结 `ToolMessage`，并写 `stale_response_blocked` 事件 |
| 缺失固定值时 | **fail-closed**：拒绝派发并说明"dispatch pinning 未生效"。宁可拦住也不放行 |
| `Toolbox.assert_control_current()` | 传入固定版本（不再自读自比），供工具在真正提交远端操作前自查 |
| `AgentRunner._assert_dispatch_allowed` | 新增 `pinned_control_version` 参数，比较固定值与当前值，不一致抛 `StaleResponseError` |

事件负载包含 `pinned_control_version` / `current_control_version`，便于复盘
"是哪一轮的响应被拦下了"。

### 验证

`tests/integration/test_control_version.py`（5 项），核心用例是**真实并发时序**：

- `VersionBumpingChatModel` 在 `_generate()` 期间用同步连接真的把
  `control_version` +1 —— 等价于"用户在模型思考期间改变方向"；
- 断言：工具**未执行**、没有 `tool_operations` 记录、
  被拦下的 tool_call 仍形成合法 `ToolMessage`、事件里 `pinned=0 / current=1`；
- 另有 fail-closed 用例与"正常轮次不误伤"用例。

> 关键点：**不**用手动 `raise` 来模拟过期。异常类型断言只证明 pytest 会抛异常，
> 不证明业务路径生效。

### 已知边界（保持不变）

`dispatch_control_version` 的作用域是**一个模型轮次**。工具结果之后的下一次模型
调用会重新固定版本（这是期望行为：新轮次基于最新状态决策）。因此最终图状态里的
字段值是**最后一轮**的固定值，判断某次派发是否被拦下要看事件，不能看最终状态。

---

## 2. 重启恢复顺序与设计承诺相反

### 问题

`run_startup_recovery` 先把 `recovering` 的 run 全部改成 `queued`，然后才调用
`reconcile_remote_jobs`。远端状态未知时也没有任何闸门阻止续跑。

后果：在没有证据证明"副作用没发生"的情况下继续跑，属于凭空假设。

### 修复

新顺序（**先核对、再决定**）：

1. `recover_stale_runs` —— 只把 lease 过期的 `running` 标成 `recovering`，
   **不做归宿决定**；
2. `reconcile_remote_jobs` —— 核对远端真实状态；
3. `decide_recovering_runs` —— 逐 run 决定：
   - 存在 `state=unknown` 或 `sync_status=uncertain` 的 job -> **保持 `recovering`**；
   - 执行岛不可达（本轮核对本身失败）-> **保持 `recovering`**
     （"没查到"不等于"没有副作用"）；
   - 其余 -> `queued`，并写 `reconciled_before_requeue` 依据事件。

阻塞的 run 会写 `stop_reason` + `input.required` 事件，列出具体未决 job，
并指明处置动作。

**用户处置入口**：`POST /runs/{id}/resume` 对 `recovering` 的 run 强制要求
`acknowledge_unknown_operations=true`，否则返回 409 并附上未决 job 清单 ——
不能让"无法核实"被一次普通 resume 悄悄盖过去。
（`RESUMABLE_RUN_STATUSES` 因此增加了 `RECOVERING`，但带额外前置条件。）

### 演练中又抓到四个缺陷（已修）

真实演练暴露的问题比单元测试多得多，这也是"必须做进程级演练"的理由：

1. **执行岛返回 404 导致整个核对崩溃**：控制端有 `tool_jobs` 记录但执行岛没有该
   job 时，`get_job` 抛 `ExecutorRejected`（4xx），而 `reconcile_remote_jobs`
   只捕获了 `ExecutorConnectionError | ExecutorProtocolError` —— Runner 直接
   以退出码 1 结束。
   修复：404 记为 `unknown`（**不**推断为"没发生副作用"：可能是执行岛重启/台账清理，
   也可能是从未接收，无法区分），并补上 `ExecutorRejected` 与兜底 `except`。
2. **单个 job 的意外中断整次核对**：现在每个 job 独立处理，异常只影响该 job。
3. **核对是串行的**：每个 job 都要经 SSH 隧道往返一次。演练时 `tool_jobs` 里积压了
   上百条记录，串行核对把恢复流程拖到几分钟，Runner 迟迟进不了正常调度
   （第一次演练就是因此等不到目标 run）。
   修复：改为**有界并发**（`JOB_RECONCILE_CONCURRENCY = 8`），这是资源初值，
   不是任务总时长限制。
4. **`recover_stale_runs` 的 stale 判定与文档承诺不一致**：实现用
   `now - run_lease_seconds` 当截止，等价于"lease 过期之后又过一整个 lease 周期
   才算 stale"，把恢复整体推迟一个 lease 周期。演练里出现"进程早已被杀、run 却仍
   不算 stale"就是这个问题。
   修复：判定改为 `lease_until < now` —— 与"lease 过期即恢复"的口径一致。

另外给 `workbench runner` 增加 `--only-run-id`（定向驱动）与 `--no-recover`：
数据库里堆着其它 queued run 时，调试/演练必须能确定性地只处理目标 run，
否则会被前面的 run 无限期拖住。这不是演练专用后门，而是正常的可操作性需求。

### 验证

`tests/recovery/test_recovery_order.py`（8 项）+ `tests/recovery/test_reconcile_failures.py`
（2 项，用执行岛替身制造 404 与意外异常）+ `tests/recovery/test_recovery.py`
（13 项，其中迟到响应用例已改为**调用真实派发路径**）
+ `tests/integration/test_resume_api.py`（3 项，走真实 HTTP 层验证 resume 的确认要求）。

**进程级演练**：`scripts/restart_drill.py`（最近一次 **通过**）

1. 在独立进程启动真实 Runner，让它真的把 `/slow?delay=8&chunks=4` 派发到执行岛；
2. 观察到在途任务后 `kill()` 该进程（硬杀，非优雅退出）；
3. 等 lease 过期，跑真实启动恢复并打印核对报告；
4. 重启 Runner（`--once --only-run-id`），确认不会自行续跑被阻塞的 run；
5. 用**目标端服务端计数器**证明 `/slow` 只执行过一次。

最近一次演练的关键观察：

```
观察到在途任务：28420f22-…:received
killed=True exitcode=1
被杀后 run.status=running owner=runner-local-owner lease_until=…03:58:42+00:00
恢复后 run.status=queued
重启后 run.status=completed（核对通过后允许继续，属预期）
/slow 实际执行次数（本次演练新增）：1      ← 无重复副作用
```

判据（脚本自动检查，任一不满足即退出码 1）：
- lease 过期后该 run 进入 recovering 流程；
- 恢复流程对该 run 作出**明确**决定（允许续跑或阻塞，不能跳过）；
- 若被重新排队，必须有 `reconciled_before_requeue` 依据事件；
- 若恢复判定为阻塞，重启后不得自行推进；
- 目标端 `/slow` 新增执行次数 **恰好为 1**。

证据落盘在 `data/drills/<timestamp>/`（`recovery-report.json`、两份 runner 日志、
`drill-summary.json`）。

---

## 3. 标准验收命令不能原样复现

### 3.1 `uv run pytest` 收集失败

**现象**：`uv run pytest tests/unit tests/integration -q` 报
`ModuleNotFoundError: No module named 'tests'`；而 `python -m pytest ...` 正常。

**根因**：`uv run pytest` 走的是 console script，**不会**把 CWD 放进 `sys.path`；
`python -m pytest` 会（`-m` 会把 CWD 加入）。测试里的 `from tests.support...`
因此在两种入口下行为不同。

**修复**：
- `[tool.pytest.ini_options] pythonpath = ["."]`；
- 显式补 `tests/__init__.py` 与各子目录 `__init__.py`，让 `tests` 成为真正的包。

**验证**：`uv run pytest tests/unit tests/integration -q` → 通过。

### 3.2 `workbench doctor` 在默认 Windows 输出编码下崩溃

**现象**：在 cp936 控制台下 `AttributeError: 'NoneType' object has no attribute 'strip'`
（来自 `_check_encoding`），并伴随一段乱码回溯。

**根因**（两步）：
1. `subprocess` 的子进程按 locale（cp936）写 stdout，父进程按 UTF-8 解码 ->
   读线程抛 `UnicodeDecodeError`；`subprocess` 会吞掉读线程异常，
   于是 `proc.stdout` 变成 `None`，真正的错误被掩盖成 `AttributeError`；
2. CLI 自身 stdout 是 gbk，中文输出随时可能 `UnicodeEncodeError`。

**修复**：
- `platform_compat.child_process_env()`：给**所有**子进程注入
  `PYTHONUTF8=1 / PYTHONIOENCODING=utf-8`（编码检查与 `pg_dump` 都改用）；
- `platform_compat.configure_stdio_utf8()`：入口把自身 stdout/stderr 切到 UTF-8，
  `--errors=replace` 兜底；
- 读取子进程输出统一加 `errors="replace"` 并容忍 `stdout is None`。

**验证**：`PYTHONUTF8= PYTHONIOENCODING=cp936 uv run workbench doctor` →
`encoding: PASS`，无崩溃、无乱码。

---

## 4. 一条经验（写进 import hygiene 测试）

开发期间我**四次**把 `EventType` 从 `agent_workbench.contracts.enums` 导入
（它实际在 `contracts.events`），每次都要等到 pytest 收集阶段才暴露。

已新增 `tests/unit/test_import_hygiene.py`：用 AST 扫描全仓库导入语句，
禁止从错误模块导入协议类型；同时验证全部控制端与执行岛模块可导入
（顺带捕捉循环导入）。

---

## 5. 本轮未改动、保持关闭的能力

- **通用命令无人值守模式**：仍然关闭。执行岛 `network_isolation=unavailable`，
  在具备执行环境级出站约束并通过验证前不会开启。
- 浏览器（501）、报告（501）、前端控制台（未建立）：与之前一致。

## 复现

```powershell
uv sync --frozen --all-packages --group dev
uv run ruff check . ; uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration -q
uv run pytest tests/recovery -q
uv run workbench doctor
# 需要真实模型与可达执行岛：
$env:XIAOMI_API_KEY = "<本机密钥>"
uv run python scripts/restart_drill.py
```

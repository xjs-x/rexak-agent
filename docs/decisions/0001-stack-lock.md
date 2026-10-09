# 0001 · 技术栈锁定与框架兼容性结论

- 状态：**已锁定**
- 日期：2026-10-08
- 依据：BUILD_PLAN P0「建仓库、锁依赖、验证框架与模型兼容性」

## 1. 结论摘要

框架选型成立。用锁定版本在**真实 PostgreSQL** 上验证了以下能力，全部通过：

| 能力 | 结论 | 证据 |
|---|---|---|
| `create_agent(model, tools, system_prompt, middleware, state_schema, context_schema, checkpointer, store)` | 可用，签名与方案假设一致 | `tests/integration/test_framework_compatibility.py` |
| 自定义 `state_schema`（继承 `AgentState`，含自定义 reducer） | 可用 | 同上：`test_custom_state_reducer_merges_id_sets` |
| `messages` 由框架 `add_messages` reducer 管理 | 可用，无需自建消息协议 | 同上：`test_agent_loop_calls_tool_and_persists_state` |
| middleware hook（`before_model` / `after_model` / `wrap_model_call` / `wrap_tool_call`） | 可用 | 生产代码 `agent/middleware/*`，被集成测试覆盖 |
| 工具返回 `Command` 更新图状态 | 可用，但**必须**在 `Command.update` 中带上对应的 `ToolMessage` | `tools/task.py`、`tools/completion.py` |
| `ToolRuntime` 注入（含 `context`、`store`、`tool_call_id`） | 可用；`ToolRuntime` 参数**不会**出现在 `tool_call_schema` 里 | `tools/http.py` |
| `AsyncPostgresSaver` + `AsyncPostgresStore`（官方 PostgreSQL 实现） | 可用，schema 隔离生效 | `persistence/bootstrap.py` |
| checkpoint 跨进程重启按同 `thread_id` 读取 | 通过 | `test_checkpoint_is_readable_in_same_thread_across_compilations` |
| Store 按 namespace 隔离、跨 thread 查询 | 通过 | `test_store_isolated_by_namespace` |
| `stream_mode=["messages","updates"]` 增量流 | 通过 | `test_streaming_emits_message_chunks` |
| `SummarizationMiddleware` 摘要中间件 | 可用，但**必须用绝对 token 阈值**（见 §4.2） | `agent/middleware/compaction.py` |

## 2. 实际锁定的版本

`uv.lock` 已提交，共 91 个包。Python 由 `.python-version` 锁定为 `3.12`，实际解析为
**CPython 3.12.14**（uv 管理）。

| 包 | 版本 |
|---|---|
| `langchain` | 1.4.3 |
| `langchain-core` | 1.6.7 |
| `langgraph` | 1.2.14 |
| `langgraph-checkpoint` | 4.2.0 |
| `langgraph-checkpoint-postgres` | 3.1.2 |
| `langchain-openai` | 1.6.7 |
| `fastapi` | 0.143.0 |
| `starlette` | 1.7.0 |
| `pydantic` | 2.14.0 |
| `pydantic-settings` | 2.15.0 |
| `sqlalchemy` | 2.1.4 |
| `alembic` | 1.20.0 |
| `httpx` | 0.28.1 |
| `psycopg` | 3.3.6 |
| `psycopg-pool` | 3.3.3 |
| `asyncssh` | 2.24.1 |
| `structlog` | 26.1.0 |
| `typer` | 0.27.3 |
| `uvicorn` | 0.54.0 |
| `playwright` | 1.63.0 |
| `aiosqlite` | 0.22.1 |
| `pytest` | 9.1.1 |
| `pytest-asyncio` | 1.4.0 |
| `ruff` | 0.16.10 |
| `pyright` | 1.1.414 |
| `respx` | 0.23.1 |

Git 仓库：`rexak-agent`。Python 包名：`agent_workbench`（控制端）、`workbench_executor`（执行岛）。
CLI：`workbench`（控制端）、`workbench-executor`（执行岛）。

## 3. 真实模型能力（小米 `mimo-v2.6-pro`）

由 `agent_workbench.models.probe` 用**无副作用本地工具**实测，端点
`https://api.xiaomimimo.com/v1`。**结论不写进代码常量**，由 `workbench doctor`
每次现测；这里只记录本次结果。

| 探针 | 结果 |
|---|---|
| 纯文本响应 | PASS（2 字符响应，1807 ms） |
| 完整工具参数组装 | PASS（`{"label":"mid-check","count":2,"tags":["alpha","beta"]}` 完全正确） |
| 流式 | PASS（74 个分片，首 token 1522 ms） |
| **流式 tool_call 分片拼装** | PASS（11 个分片拼出合法 JSON） |
| 空输入处理 | PASS（返回而非抛错，10329 ms） |
| `usage` 字段 | PASS |
| 图片支持 | `unknown`（未验证，**不**从模型名推断） |

### 3.1 实测 usage 字段口径（影响费用计算）

小米返回的 `usage_metadata` 形如：

```json
{
  "input_tokens": 144,
  "output_tokens": 49,
  "total_tokens": 193,
  "input_token_details": {"cache_read": 0},
  "output_token_details": {"reasoning": 23}
}
```

因此 `observability/costs.normalize_usage` 显式支持
`input_token_details.cache_read` 与 `output_token_details.reasoning` 两条路径。
按 BUILD_PLAN §11.3 规则 1，缓存 / 推理 token **可能已包含在总量里**，
是否扣除由 `ModelPrice.cache_tokens_included_in_input` 决定，不默认相加。

### 3.2 首版价格目录为空

`default_price_catalog()` 返回空目录。**这是刻意的**：不臆造小米单价。
结果是所有模型调用以 `cost_status=unpriced` 记录 tokens 而**不显示为 0 成本**
（ACCEPTANCE A28）。用户可在设置中写入真实价格并产生新的价格版本。

## 4. 与方案不同或需要额外说明的决策

### 4.1 Windows 控制端必须使用 Selector 事件循环

psycopg 的异步连接在 Windows 默认的 `ProactorEventLoop` 上会直接抛
`InterfaceError`。控制端支持的部署形态是「Windows 控制端 + Linux 执行岛」，
因此**所有入口**（CLI 各子命令、API、Runner）统一调用
`agent_workbench.platform_compat.configure_event_loop_policy()`。

这不是临时补丁：它被放在 `platform_compat` 并覆盖全部入口，避免"某个入口忘了设"。

### 4.2 `SummarizationMiddleware` 用绝对 token 阈值

`trigger=("fraction", 0.6)` 会要求模型提供 `max_input_tokens` profile，而小米这类
兼容接口默认拿不到，构造时直接抛 `ValueError`。

方案 §7.2 的要求本就是"上下文阈值**按模型配置**"，所以改为由
`ProviderProfile.input_budget_tokens × context_trigger_ratio` 算出**绝对 token 数**，
再传 `trigger=("tokens", N)`。可调比例仍是 `WORKBENCH_CONTEXT_TRIGGER_RATIO=0.6`，
并明确标注为**可调初值，不是通用最佳值**。

### 4.3 工具异常在 middleware 层转成结构化结果

`create_agent` 的默认行为是**把工具异常向上抛**（已用
`test_raw_create_agent_propagates_tool_error` 固定该事实）。这会让"某个工具失败"
升级为"整个 run 失败"，掩盖真实原因。

因此 `ToolDispatchMiddleware.awrap_tool_call` 统一捕获并按类型分类
（`validation_failed` / `timeout` / `internal_error`），写成合法的终结
`ToolMessage`。`asyncio.CancelledError` **不捕获**，仍向上传播。

### 4.4 执行岛 Python 版本放宽到 3.13

执行岛（Kali rolling）自带 Python 3.13.12，且岛上**无法访问 GitHub**
（连接被重置），无法安装 uv 管理的独立 CPython 3.12。

处理方式：`requires-python` 放宽到 `>=3.12,<3.14`，`.python-version` 仍是 `3.12`
（控制端开发与锁依赖基线不变）。Executor 不依赖 LangGraph，无需与控制端保持
完全一致的补丁版本。详见 decision 0002。

### 4.5 `alembic.ini` 必须保持纯 ASCII

Alembic 用 `encoding="locale"` 读取该文件；在 GBK locale 的 Windows 上，
中文注释会导致 `UnicodeDecodeError` 而**无法启动**。该文件因此保持纯 ASCII，
并有单测 `test_alembic_ini_is_ascii_only` 守住这条约束。

### 4.6 `tools/__init__.py` 刻意不导出任何符号

工具模块需要 `agent_workbench.agent.context` 做类型标注，而 `agent.context` 又依赖
`agent_workbench.tools.runtime`。在包 `__init__` 里急切导入子模块会形成循环导入。
因此该文件只保留文档，调用方直接从子模块导入。依赖方向始终是
`agent -> tools -> execution/domain`。

## 5. 未验证 / 明确关闭的能力

- **图片输入**：`unknown`，未做任何声称。
- **浏览器**：Executor 对 `/browser-sessions` 返回 **501**，不是假实现（P4 交付）。
- **通用命令的无人值守模式**：**已关闭**。执行岛 `network_isolation=unavailable`，
  缺少执行环境级出站约束；`/capabilities` 如实返回
  `command_unattended_allowed=false`。正则黑名单、工作目录与提示词都**不构成**网络隔离。
- **报告生成**：`POST /api/sessions/{id}/reports` 返回 **501**（P6 交付）。
- **多货币折算**：不折算；不同货币分开汇总。

## 6. 复现方式

```powershell
uv sync --frozen --all-packages --group dev
docker compose up -d postgres
uv run workbench db-init
uv run workbench doctor
uv run pytest tests/unit tests/integration tests/recovery -q
```

# 第二版工程验收与补修

日期：2026-10-14。工作目录：`C:/xjs/rexak-agent`。分支：`codex/implementation-v1`。

验收基线为 `3cb370e4412d51253c402404588d5dac5c94f125`。此次在该提交上复核、补修和回归六批功能；不访问 S2-045、Tomcat8、Earth，不读取或展示原诊断包的敏感自由文本。真实效果实验由用户执行。

## 1. 验收结论

**第二版工程实现通过本次验收，可以进入用户真实复测。** 原提交的六批功能确已实现，但存在运行接缝缺口，不能仅凭原有测试通过直接接收。此次已修复下表问题，并增加能覆盖实际调用链的回归。

本机对原提交复跑 `tests/unit + tests/integration + tests/recovery` 得到 **424 passed / 3 skipped**，没有复现交接消息中的“437 项全绿”口径。两次 Windows 符号链接测试及未配置真实模型凭据的兼容性测试明确跳过，不能算通过；此差异不用于推断原交接测试是否在不同环境执行。

## 2. 发现与修复

| 批次 | 复核发现 | 已实施修复 | 主要证据 |
|---|---|---|---|
| A | 恢复额度耗尽后重启仍执行一次初始请求，重复重启可无限增加请求；额度读取错误还会归零 | 按已登记的物理尝试数计算剩余额度；包含 pending；耗尽零请求；额度读取和必须持久化的登记失败时不发送 | 重启三次台账仍只有三次尝试；数据库读取失败时 handler 零调用 |
| A | ContextBuilder 未向预算函数传入恢复额度，实际输出设置与预算不一致 | 从供应商适配器最终 wire 参数读取实际额度；校验配置；配置快照包含独立输入上限 | 实际捕获请求 1,000→16,000，末次 manifest 额度 16,000、允许输入 30,600；每次用量独立入账 |
| A | 辅助调用可能接收有正文的截断输出；重复 tool_call ID 未被拒绝 | 辅助调用入账后拒绝不完整摘要/经验；重复 ID 拒绝整批；纯空白按空响应处理 | 合成 compaction/memory_distillation 截断被拒；完整响应与正常聊天回归 |
| B | 模型 method 可覆盖采集方法；显式来源会丢弃其他引用来源；身份标签可能掩盖绑定身份 | 采集方法优先，冲突明确反馈；保留全量来源集合；拒绝引用无关操作；优先使用真实 identity_id | 来源解析与冲突用例；原有双事务资产并发、重放验收继续通过 |
| C | 自动刷新覆盖显式焦点、lead_id、返回主线位置；允许显式选择完成/受阻步骤和不存在的返回步骤 | 保留有效显式焦点；清理失效焦点；校验步骤及返回位置 | 修复前焦点/返回位置回归失败，修复后通过；原工作上下文回归 |
| C | v2 迁移没有执行 v1 已有的未提交并行结果保护；判断引用未经可用性检查 | v2 同样拒绝不安全迁移；轻量判断复用现有工件归属/可用性检查，不静默截掉超量引用 | v2 pending-result 拒绝；原官方 checkpoint 待执行节点及 1→2→3 测试通过 |
| D | 所有失败 job 都写 response/delivered，包括超时和本地构造失败；303 没有启用移除实体头标记 | 按稳定错误码区分构造、传输、未知状态；保守记录副作用不确定性；303 去掉实体头 | 控制端实际组装 ToolResult 的构造/超时/连接错误用例；本地回显检查 GET 无 Content-Type |
| D | 两端 multipart 空元数据行为不一致，字节上限实际按字符算；名称被静默改写；旧 raw 表单 CSRF 路径被拒 | 两端统一空值和 UTF-8 字节限制；转义名称；禁止 CR/LF/NUL 元数据；恢复已有字符串表单 CSRF；原始 bytes 元数据准确 | 两端契约用例；独立回显；Executor 真实 API/台账重放；原 CSRF job 测试 |
| E | 相似签名包含累计操作计数，正常执行每次都改变；身份关联 SQL 将右值绑定为 JSONB，运行报错 | 结果类别取最近完成操作，不含累计计数；保留有界 HTTP 结果类别；关系检索使用合法 JSONB contains | 新增一次同类操作签名仍相同；获得不同身份时可召回相关旧线索 |
| E | 未接入 asset 关系、当前身份，关键词仍用整句；保留候选可能占满检索位置 | 关联资产和身份优先；保留关系/轮转位置；有限关键词兜底；判断变化重置软提醒 | 身份召回与既有 207 条旧线索轮转验收通过 |
| F | 焦点切换后旧判断仍显示为当前下一步；不同排序下末尾 UUID 比较误报新记录，遗漏修订变化 | 上下文、报告、前端标明历史判断；覆盖位置按 ID/版本集合比较 | 无压缩首份判断报告；旧判断适用性；顺序变化不误报，修订增大能发现 |
| 展示 | 页面未订阅响应质量事件，归档中的截断文本无标记 | 质量事件关联 model_call_id/response_id；对话只读接口提供 outcome；前端标示片段并订阅新事件 | 真实 API 返回 truncated/usable；OpenAPI 重新生成，前端构建通过 |

第一组补充用例在修复前 **11 failed**，覆盖恢复重启、实际预算、显式焦点、返回位置、multipart 两端行为及相似签名。这些失败是定位证据，不纳入最终通过数量。

## 3. 最终检查

| 检查 | 结果 |
|---|---|
| `uv sync --frozen --all-packages --group dev` | 通过 |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 0 errors / 0 warnings |
| `uv run python scripts/check_encoding.py` | 307 个受检文件通过；本次改动另行核对 UTF-8 无 BOM |
| `uv run pytest tests/unit tests/integration tests/recovery -q` | 448 passed / 3 skipped / 12 既有警告 |
| `uv run pytest executor/tests -q` | 26 passed / 20 skipped |
| `pnpm --dir web generate:api` | 根据当前 OpenAPI 生成类型 |
| `pnpm --dir web build` | TypeScript 与 Vite 构建通过 |
| `git diff --check` | 通过 |

Executor 测试仅访问临时本地回显服务、自签名 HTTPS 服务和仓库自带 `target_app.py` fixture；该 fixture 在测试前确认端口空闲后启动，完成后停止。没有替换用户已有服务。

Executor 的 20 项跳过属于 Windows 无法验证的 Linux 符号链接/进程组语义，不宣称本次重新通过了这些平台专项。控制端三项跳过与基线一致。测试中仍有 12 条既有 Pydantic 警告，来自测试用 SimpleNamespace 替身的序列化；没有将它们计为功能验证结果。

## 4. 验证范围与测试文件

本次新增：

- `tests/unit/test_v2_acceptance_regressions.py`：实际预算、焦点、返回点、两端字节约束、判断适用性、报告版本变化、重复调用 ID。
- `tests/integration/test_v2_acceptance_paths.py`：最终 wire 捕获、台账与质量事件、辅助调用、迁移、无压缩报告、身份召回、HTTP 错误阶段、对话归档。
- `executor/tests/test_structured_http_jobs.py`：真实 Executor API、能力声明、编码元数据、同操作重放只发送一次。

同时增强原有恢复重启、相似签名、303 实体头测试；保留原有并发资产与 checkpoint 回归。

没有引入第二套 Agent 主循环；checkpoint、领域表、Executor 台账仍各自管理权威状态。没有新增任务总时间/费用上限，也没有加入任务专属路径或目标答案。新增错误检查针对此次可复现的工程故障，没有增加逐轮反思模型或独立漏洞审查矩阵。

## 5. 用户真实复测前的生效步骤

先把本次控制端与 Executor 代码分别部署到对应环境，然后重启各自服务，使装配的模型/提示词/middleware 与新代码一致。仅更新磁盘文件或源码指纹不证明旧进程已加载新实现。

Executor 的 `/capabilities` 应声明 `form`、`multipart`；否则控制端会明确拒绝结构化请求。控制端模型配置需记录正常输出额度、恢复输出额度和最大恢复次数。恢复额度默认不提高，本次没有替用户决定模型配置。

最初工程验收尚未更新 Kali 服务；后续已按用户要求补齐两端部署，结果见文末。本次没有调用真实模型。用户启动真实实验时记录两端版本、模型配置、任务原文、靶场重置及人工介入。具体三组观察点仍见[第二版方案](../../implementation-v2-plan.md#10-用户执行的三组真实复测)。

工作树既有 `.workbuddy` 修改及 `docs/experiments/002-v1-three-targets-2026-10-13/` 原复核材料保持独立，不进入补修提交。

## 最终确认记录

最终控制端全量回归完成：**448 passed / 3 skipped**，耗时 180.73 秒；Executor 全量回归 **26 passed / 20 skipped**。新增控制端验收用例 24 项，以及 Executor API 重放用例 2 项，均已计入对应最终数量，不重复求和。

ruff、pyright、编码检查及前端构建均通过。上述结果只证明本报告列出的工程行为，不替代用户即将进行的真实安全测试效果验收。补修以独立提交落在原分支，基线提交保留不变。

## 两端部署收尾（用户新增约定）

用户要求后续验收自动包括 Kali 代码更新、服务重启和原桌面入口验证，该要求已写入项目 AGENTS.md。首次连接因 Kali SSH 超时未部署；用户启动 Kali 后，本轮已完成收尾：

- 部署到发布目录 `20261014T022452921353Z`，15 个上传文件哈希验证通过；离线 wheel 安装完成，Executor 停止后重新启动，`pip check` 通过。
- 对实际安装包的 11 个 Python 文件逐项核对 SHA256，与当前仓库执行端源码一致。运行进程 PID `1771563`，boot_id `1771563-436e7db3c4bc`；执行端身份与控制端配置一致，能力声明包含 raw/json/form/multipart。
- 在 Kali 临时回环回显服务上，通过运行中的 Executor API 验证 form/multipart：独立解析正文、重复字段顺序、请求字节数和 SHA256 均通过。重复提交返回同一 job；两种请求共观察到两次实际发送。
- 用现有启动脚本进行 API-only 启动检查：数据库、SSH、Executor 和 API 就绪；控制台及新版 JS bundle 返回 HTTP 200。检查结束后停止本次 API 进程，保留数据库和 Kali Executor。检查未启动 Runner，也未调用真实模型或靶场。
- 桌面 **Rexak Agent** 快捷方式仍指向 `C:/xjs/rexak-agent/start-workbench.cmd`。该入口调用当前仓库脚本并加载当前源码，不需重新打包。用户直接双击、按提示输入模型 Key 即可运行新版。

**运行环境部署已完成**。元数据证据见 [deployment-evidence.json](deployment-evidence.json) 和 [SHA256 清单](deployment-evidence.sha256)。证据不包含凭据、Cookie、请求正文、隐藏推理或靶场答案。

# V1 实施进度与复核入口

> 历史设计/实施记录：不代表 v1.0 当前功能范围。passive/Shadow/Progress 路径已退役；当前实现与验收见 [v1.0 收尾报告](../reports/v1-finalization.md)。

日期：2026-10-13。对应 [第一版方案](implementation-and-experiments-v1.md) 第 12 节的六阶段顺序。

六阶段代码已完成最终本地工程验收；累计实现统一冻结到本地提交，未伪造六个各自可运行的历史提交。真实实验由用户执行，本轮没有连接靶场或调用真实模型。启用、独立试验和复盘入口见 [操作说明](experiments/v1-operation-guide.md)。

## 本轮改动

- 固定 B0 为 `a37ae3786ab495035bbe14bd50276a7553438995`，源码内容与依赖哈希见 [基线登记](experiments/v1-baseline.json)。指纹读取 Git 原始 blob，避免 `git archive` 的换行转换影响 commit 内容哈希。当前工作树指纹单独保存，导出机的源码不冒充 run 创建时源码。
- 开始修改前的工作树清单保存于 `data/implementation-v1/baseline-before-implementation.json`；该私有文件包含原有三个未提交路径及其哈希。原 `.workbuddy` 修改和方案文档均保留。
- `workbench export-run --bundle` 导出选定 run 所在会话的全部 run、保留事件、模型调用、操作、报告、观察、当前领域状态及最新 checkpoint 只读投影。旧 `export-run` 继续按单个 run 导出。
- 数据库使用只读 repeatable-read，保存读取时间、会话版本及事件游标。checkpoint 保存读取区间、ID、时间和 channel versions。两者和文件读取明确标为非原子组合快照；导出时点状态不代表历史模型输入或旧 run 结束时状态。
- 复制来源明确的工具工件及报告原件，按复制后的字节检查 SHA-256 和大小。损坏、不可用、缺元数据或无法确认工具/报告来源时逐项登记，不复制模型协议归档或隐藏推理正文。
- 生成 `bundle-index.json` 和 `checksums.sha256`，`workbench verify-bundle` 检查大小、哈希、缺件、额外文件和路径逃逸。整个目录先构建再发布，已存在的包拒绝覆盖。包内 `.gitignore` 阻止普通 `git add` 收录原件。
- `TrialRegistration` 校验操作者登记、会话与 run 集合；时间要求带 UTC 偏移，保存 UTC 与 `Asia/Shanghai` 本地时间。模型配置中的正常 token 上限保留，已知秘密字段与隐藏推理字段脱敏。未知用量继续保持 `null`。
- 完整请求预算复核覆盖系统提示、工具 schema、参数、结果及供应商协议字段，保留输出预留与安全余量；估算方法明确标为不精确。实际请求前保存 manifest，包含消息清单、来源和上下文投影。超限时先归档正常可见工具正文，保持 tool-call 配对；仍超限则进入恢复，不发送请求。
- 压缩保存正常可见脱敏来源，分块请求使用公共观测层；每个物理尝试单独记录用量。结构化摘要刷新当前判断、失败方向和待办，目标及约束保留。失败不会改写原历史；取消和迟到响应不注入当前决策。
- 主循环与辅助调用共用观测层，SDK 重试关闭。台账写入失败保存待同步终态，`sync-model-observations` 按原物理调用 ID 幂等补齐，不重新调用模型。封包包含真实可用 manifest 与来源，缺少的历史请求继续明确标记缺失。
- 完整模型—工具轮次闭合后，默认每 4 轮主动让出。Runner 从已提交 checkpoint 在同一 run/thread 接续，不添加虚构用户输入、不重置计划。并行工具未闭合不能让出；暂停、新输入、租约和控制版本检查保留。
- `update_plan` 同时更新计划和简短焦点，补充完成条件、恢复说明、证据引用。只读黑板从 checkpoint、领域表与真实 job 台账组装；主线及最多三条备选保留旧线索轮转位置，分页范围覆盖全部未完成线索。游标在实际请求成功返回后提交，展示不冒充有效复评。
- `search_history` 对同一 session 的线索、观察、操作摘要、报告来源、正常可见历史和计划作关键词/中文子串检索，返回版本、来源和下一页游标。归档来源可展开，原始工具工件仍由操作台账追溯；不检索隐藏推理。
- `record_lead` 可同一事务联记关键观察及线索别名，验证工件归属和哈希。合并保留来源和状态，拒绝跨 session 与循环。工具回执按 run/tool_call_id 去重，重放返回原结果；执行上下文保存当轮焦点和计划版本。
- ProgressMiddleware 在完整工具结果后的决策边界计算观察、入口、线索下一步和关键操作变化，排除模型归档、报告和记忆。固定 streak 自动停机改为有冷却的软提醒，不按文件哈希声称新知识。
- 状态 v1→v2 使用官方更新接口显式迁移，保存来源 checkpoint 与预期节点；消息、计划和摘要来源保留，活跃引用重投影。含未提交并行结果、未知版本或边界变化的旧状态明确拒绝自动恢复，避免猜测重放。
- 项目事实、owner 级方法经验及反馈同时支持 trial 读写隔离。已有主模型简短记录可形成带适用/失败条件的候选；来源及使用事件去重，不累加重放成功次数、不自动升级验证状态。阶段完成、失败及暂停时提炼已有有来源观察，不新增逐轮提炼模型调用。
- 关键领域转折、子目标完成和暂停/终态生成阶段报告。普通工具输出不触发；失败保留原版本及快照可重试，已发布版本不重复渲染。无 confirmed 也能交付；确认发现仍受原验证和证据规则约束。
- 完成提议按声明的目标相关未决项及具体搁置理由检查，不再按分数≥60 自动阻断。真实未决操作、用户指令和证据一致性仍检查；多次拒绝继续反馈具体问题，不按拒绝次数强制用户介入。
- `prepare-trial` 只生成私有启动配置，拒绝未冻结源码、登记哈希不符或覆盖已有目录；各 trial 独立数据库、数据根及记忆。B0 原始 schema 可直接只读导出，缺失的新字段明确为 null，不迁移试验库。`analyze-bundle` 验哈希后重建时间线、用量、估计误差、召回与重复候选，实际结果/准确性/全站召回率保持未知。

## 使用已实现的命令

```powershell
Set-Location 'C:\xjs\rexak-agent'
uv run workbench export-run <RUN_ID> --bundle --output 'C:\xjs\rexak-agent\data\real-experiments\exports'
uv run workbench verify-bundle 'C:\xjs\rexak-agent\data\real-experiments\exports\<RUN_ID>\bundle'
```

可选 `--trial <JSON 文件>` 用于操作者的实验登记；必须配合 `--bundle`。字段与 [登记契约](../src/agent_workbench/contracts/experiments.py) 一致，例如：

```json
{
  "trial_id": "T01-V1-1",
  "environment_alias": "T01",
  "group": "V1",
  "repetition": 1,
  "environment_snapshot_id": "操作者记录的初始快照标识",
  "memory_snapshot_id": "操作者记录的隔离记忆标识",
  "started_at": "2026-10-13T08:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

这只是填写格式，当前代码不是冻结的 V1，不能用该示例登记正式 V1 样本。未填写的环境、源码、终止、人工介入等字段保持未知。操作者提供的源码哈希明确标为未独立验证；如果续聊前后代码或配置有变化，按各 run 的记录与操作者笔记复核，不能仅依赖顶层单个哈希。

证据原件可能含敏感自由文本，包仅存本机私有目录。哈希证明文件一致性，不判断测试成功。`operator-notes.jsonl` 与 `evaluation-private.json` 是空白占位，当前命令不收集人工笔记或私有结果项。封包后修改它们会触发哈希不一致，应另存操作者记录并保留原包。

## 验证记录

最终 `uv run pytest tests/unit tests/integration tests/recovery -q --tb=short`：**363 passed、3 skipped**。跳过两项 Windows 符号链接权限用例与未配置凭据的真实小米用例（NOT_RUN）。脚本工具上下文替身产生 12 项 Pydantic 序列化提醒，不影响协议测试；不作为真实运行证据。Ruff、Pyright（0 errors）、UTF-8 无 BOM 检查与 `git diff --check` 通过；前端 OpenAPI 生成、类型检查与生产构建通过。最后补充的 B0 原 schema 只读导出及导出回归为 **10 passed**，启动登记一致性专项为 **2 passed**。单元和集成 fixture 使用独立临时库，新增 B0 回归另建随机数据库，均已清理；没有迁移日常工作库。

本地冻结分支为 `codex/implementation-v1`；具体 commit 见 `git log -1`，完整 commit/树/依赖指纹收据保存于私有 `data/implementation-v1/v1-freeze.json`。后续源码变化须重新登记，不把变化后的版本混入同一冻结样本。原 `.workbuddy` 修改和方案文档字节保留，`.workbuddy` 不进入冻结提交。

第 2、3 阶段最终联合回归：`uv run pytest tests/unit tests/integration tests/recovery -q --tb=short` 为 **346 passed、3 skipped**；Ruff、Pyright（0 errors）、UTF-8 编码检查均通过。新增测试覆盖实际官方图与 HTTP 捕获的最终预算、压缩分块、并发隔离、迟到响应、待观测补齐，以及 11 次操作跨片段及断开 saver 后恢复；均使用脚本模型和隔离数据库，无真实靶场或付费模型调用。

以下保留第 1 阶段验收记录：

- `uv sync --frozen --all-packages --group dev`：通过，依赖未升级。
- `uv run ruff check .`、`uv run pyright`、编码检查及 `git diff --check`：通过。
- `uv run pytest tests/unit -q`：167 passed、2 skipped；跳过 Windows 符号链接权限用例。
- 最终 `uv run pytest tests/unit tests/integration -q --tb=short`：287 passed、3 skipped；除上述 2 项外，真实小米凭据用例标为 NOT_RUN。
- `uv run pytest tests/recovery -q -x --tb=short`：42 passed，全部执行，无失败。
- 前端 OpenAPI 类型生成、`pnpm typecheck`、`pnpm build`：通过。
- 新增导出专项最终 12 passed，包含真实 CLI 子进程、隔离数据库续聊范围、其他会话排除、checkpoint 读取与快照不被后续修改污染。首次环境未就绪、测试样本违反活动 run 唯一约束及指纹状态字段位置错误均保留为先前失败；环境就绪并修正后才取得上述最终结果。

## 阶段状态

| 阶段 | 当前状态 |
|---|---|
| 1. 基线、导出协议、事件 | 已实现并通过本地工程验收 |
| 2. 完整请求预算、压缩、公共模型调用观测 | 已实现并通过本地工程验收；估算校准仍需真实实验数据 |
| 3. 完整边界主动分段、恢复、暂停回归 | 已实现并通过本地工程验收 |
| 4. 黑板、焦点、分页检索、软提醒、旧线索召回与合并 | 已实现；207 条线索、来源与重放、旧 checkpoint 专项通过 |
| 5. 方法经验、阶段报告、完成提议适配 | 已实现；隔离、幂等反馈、原快照重试、连续拒绝后修正专项通过 |
| 6. 实验配置、离线复盘、前端最小展示与操作说明 | 已实现；启动配置、离线未知保留、包损坏拒绝及前端构建通过 |

旧 manifest 和旧阶段快照不能反推补造。新报告边界保存精简投影，不代表每一历史状态都可重建；数据库和 checkpoint 不是统一事务。Runner 源码指纹对应接管时文件，不冒充创建时或已加载字节码。输入 token 估计标为不精确；真实模型及靶场收益仍待用户实验。

当前限制：关键词不同可能漏召回；稳定轮转只保证全量可分页，不能保证新线索持续到来时旧线索很快被看见。图迁移不自动处理未提交的并行结果。辅助经验提炼使用已有事实投影，候选方法主要由主模型已产生的简短记录提交，没有逐轮独立反思模型。普通文字未形成结束提议时保留原有限格式提醒；结构化提议被拒没有次数上限。哈希发现意外变化，不是防篡改签名或结论正确性的证明。

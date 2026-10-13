# Rexak Agent 第二版落地代码方案

日期：2026-10-13。状态：**设计稿，尚未实施或验收**。

工作目录固定为 `C:/xjs/rexak-agent`。本方案以提交 `fba85dc1666f6be51065a1b8580c33881a441861` 的实际实现及[三次真实实验复核](experiments/002-v1-three-targets-2026-10-13/REPORT.md)为依据，承接[第一版方案](implementation-and-experiments-v1.md)。不修改旧项目 `C:/xjs/xak-agent`。

## 1. 第二版要交付的行为

程序自动维护可以确定的执行上下文，模型在关键转折时补充判断。仍使用官方 `create_agent`，不添加第二套探索循环，不要求每轮反思、打分、写报告。

| 真实问题 | 本版交付 | 不应扩大成的结论 |
|---|---|---|
| Earth 最后一次响应 length 截断，正文和工具均为空，随后无提议地进入 waiting_user | 不完整响应有明确分类、有限恢复和故障出口 | 不是证明模型探索能力不足，也不是自然收敛 |
| Tomcat8 并行登记同一入口触发唯一键冲突 | 原子登记资产，保留完整来源和调用重放语义 | 不用取消所有工具并行来解决 |
| 三次 current_focus 为空，99 条操作的步骤/线索关联为空 | 从唯一进行中步骤承接焦点，记录关联来源 | 自动关联只能说明执行时的工作上下文，不证明证据支持某个假设 |
| S2-045 手写 multipart 存在 LF-only 格式风险 | 结构化构造正常请求，明确请求实际执行阶段 | 尚不能证明请求格式就是该次阴性结果的根因 |
| 换请求会改变进展签名，关键解释依赖成功压缩 | 分开记录活动和判断变化，阶段记录独立于压缩 | 不用计数器判定安全结论或强制任务收敛 |

保留按指令运行、URL 自动应用目标、暂停后续聊、旧任务恢复。没有任务总时间或总费用上限；单次模型请求、恢复尝试、工具操作和上下文仍有资源边界。

本版真实靶场测试全部由用户执行。工程验收只使用合成模型响应、测试数据库、Executor 替身和本地回显服务。

## 2. 文件级拆分与实施顺序

下表中“新增”是计划新增文件，其余为现有文件。测试文件名是实施目标，不表示已存在或已通过。

| 批次 | 修改位置 | 具体责任 | 验收主线 |
|---|---|---|---|
| A / P0 | 新增 `models/response_quality.py`、`agent/middleware/response_recovery.py`；修改 `models/invocation.py`、`models/provider.py`、`models/context_budget.py`、`config/catalog.py`、`agent/factory.py`、`agent/middleware/model_observer.py`、`runtime/runner.py` | 响应分类、恢复、实际请求预算、逐次入账、明确故障 | 空响应后继续；恢复耗尽明确失败；没有重复执行 |
| B / P0 | `domain/assets.py`、`tools/task.py`、`tools/operation.py` | 原子资产 upsert；证据采集上下文；领域登记幂等审计 | 两个并行工具同入口均成功；重放不重复写 |
| C / P1 | 新增 `agent/focus.py`；修改 `agent/state.py`、`agent/state_migration.py`、`tools/task.py`、`tools/execution_context.py`、`agent/middleware/working_context.py`、`agent/middleware/tool_dispatch.py` | 自动承接焦点；轻量判断；保留原线索恢复位置 | 焦点可解释；并行无错误归属；暂停恢复不丢记录 |
| D / P1 | 新增 `contracts/http.py`、Executor 请求编码模块；修改 `tools/http.py`、`contracts/tool.py`、`execution/client.py`、Executor 的 `api.py`、`http_sessions.py` | 结构化 form/multipart、能力声明、构造/传输/响应区分 | 回显解析一致；重定向/身份/取消回归 |
| E / P1 | `agent/middleware/progress.py`、`agent/working_context.py`、`agent/middleware/context_builder.py`、`agent/prompts/system.py`、方法提示文件 | 活动与判断变化分离；旧线索召回；少量具体提醒 | 变参数不等于新知识；有理由的复查不被阻断 |
| F / P1 | `agent/middleware/stage_report.py`、`domain/reports.py`、`runtime/runner.py`、现有报告 API/前端投影 | 无压缩也有阶段内容；暂停/故障快照；来源时间 | 报告含当前进度、未决问题、恢复点且不虚构解释 |

控制端路径均相对于 `src/agent_workbench/`；执行端路径相对于 `executor/src/workbench_executor/`。A、B 可先交付，C 完成后接 E、F；D 独立推进。各批次保留独立提交和测试证据。

## 3. A：不完整响应恢复

### 3.1 响应完整性与网络成功分开

`invoke_attempt` 已负责单次网络尝试、用量和费用。保留其职责：供应商返回 HTTP 成功、产生 token，不代表产生了可用的 Agent 决策。

在 `models/response_quality.py` 增加纯分类器，读取可见内容、工具调用、无效工具调用以及供应商终止元数据；不读取推理正文。供应商字段先在适配层归一化。

```python
@dataclass(frozen=True)
class ResponseQuality:
    outcome: Literal[
        "usable", "truncated", "empty", "invalid_tool_call", "refused"
    ]
    finish_reason: str | None
    visible_chars: int
    valid_tool_count: int
    invalid_tool_count: int
```

判定顺序：供应商明确拒绝优先，其后明确截断、无效工具调用、空结果，最后为可用。缺少 finish_reason 但有完整可见文本或合法工具调用，不应被当成空响应。多模态内容块按适配器解析，不能通过 `str(content)` 把空结构误算成有效正文。

| 输入 | 行为 |
|---|---|
| 正常可见文本，无工具 | 交给现有完成判定；普通聊天仍可正常返回 |
| 完整合法工具调用 | 进入官方工具节点 |
| length 截断，包括看似已有完整工具的响应 | 本版保守地整批不派发，进行恢复；避免半批动作执行后又生成一批 |
| 正文空、无合法工具，也无明确拒绝 | 进行恢复；不进入泛化 waiting_user |
| 不完整 JSON、invalid_tool_calls，或同批多次 update_plan | 不执行该批；返回简短格式反馈后恢复 |
| 完整参数但业务校验失败 | 走现有 ToolMessage 错误反馈，不把全部业务错误升级成模型重试 |
| 明确拒绝 | 不自动重试以绕过拒绝；展示可见原因和现有进度 |

截断流已展示的内容标为“不完整响应”，后续请求使用不同响应 ID；不能把两次输出拼成一次回答。

### 3.2 通过官方 middleware 恢复

新增 `ResponseRecoveryMiddleware.awrap_model_call`。锁定环境中的 `langchain/agents/middleware/types.py` 明确允许 wrapper 多次调用 handler；使用这一接缝，不在 Runner 写“模型—工具”循环。

关键包装顺序：

```text
ResponseRecovery
  -> ContextBuilder：根据本次实际输出设置重新构造、预算、保存 manifest
    -> ModelObserver：为这一网络尝试调用 invoke_attempt
      -> 官方模型调用
```

压缩的 before_model hook 仍由官方图执行；单次响应恢复不手动再次调用整个图或摘要 hook。恢复仅重新构造发送视图，不删除 checkpoint 消息。必须以捕获最终请求的装配测试验证顺序，而非只检查 middleware 列表。

逻辑伪代码：

```python
for attempt in remaining_attempts(logical_call_id):
    toolbox.assert_control_current()
    request_for_attempt = recovery_request(original_request, attempt)
    response = await handler(request_for_attempt)
    quality = classify_response(response)
    await record_quality(logical_call_id, quality)
    if quality.outcome == "usable":
        return response
    if quality.outcome == "refused":
        raise ModelResponseRefused()
raise ModelResponseUnavailable()
```

伪代码中的具体异步签名以现有接口为准；实现时不能遗漏 await。默认**初次请求加最多两次自动恢复**，是单次逻辑模型决策的保护值，不是任务尝试总数限制。对于配置无变化的连续 length，可提前结束同一恢复周期，避免机械重发相同请求。

恢复反馈只写可观察的错误，例如“上次输出达到限制，未得到完整工具调用；请给出一个完整的下一步调用或简短结论”。不要求输出隐藏推理，不把供应商推理正文塞入提示词，也不要求绕过拒绝。

所有未接受的响应都在进入官方工具节点之前截住，不写成一条等待 ToolMessage 的可执行历史。可见片段、响应质量事件与费用仍可审计。

### 3.3 输出配置与预算

当前 `output_reserve` 同时用作输入预留和 `max_tokens`。本版将配置语义明确为：

```python
request_output_limit: int
recovery_output_limit: int | None
max_response_recoveries: int = 2
```

旧 `output_reserve` 配置向后兼容为 `request_output_limit`，未配置 recovery limit 时不擅自提高。支持时可把 8,192 → 16,384 作为待实验的配置组合，不能宣称它是任何供应商的默认或最佳值。

每次请求从实际生效的输出额度计算输入预算：

```text
allowed_input = (context_limit - effective_output_limit) × (1 - safety_margin)
```

`models/context_budget.py` 不再只读取固定 profile.input_budget_tokens。若模型另有独立最大输入限制，还应取其与上述预算的较小值。提高输出额度后输入不再容纳时，先使用现有有来源的正文替换机制；仍不满足则明确报预算不足，保留消息，不能静默截断目标、约束或工具协议。

供应商适配层负责额度参数名称、终止原因映射及能力限制。没有验证过的“思考预算”参数不加入通用配置。最终序列化请求的额度、预算估计和 manifest 必须一致。

### 3.4 用量、故障与重启

- 每次实际网络尝试有独立 model_call_id，共用 logical_call_id；重试次数、用量、成本与截断原因分别记录。网络成功和语义不可用是两个维度。
- 恢复上下文使用请求级 ContextVar，finally reset；不放在共享 middleware 实例的可变字段里。ModelObserver 从该上下文取得逻辑调用 ID 和恢复原因，禁止把内部字段传给供应商。
- logical_call_id 锚定同一待执行模型决策，至少包含 run、片段、稳定消息 ID 范围；不能只靠消息数量区分。自动重启从已有 model_calls 和质量事件读取已用次数，不能重新获得无限恢复额度。
- 自动恢复每次检查控制版本、暂停、取消、租约；已失效的模型响应及工具调用不再被接受。
- 恢复耗尽抛出可识别异常，由 Runner 映射为既有 `failed`，stop_reason 明确为模型响应不可用，保留 checkpoint 和领域成果。用户继续或提交新指令后可建立新恢复周期，关联上个失败周期。
- 网络超时、传输错误和用户取消继续走现有明确路径，不因本功能自动加入广泛网络重试。摘要、长期经验提炼等辅助调用必须拒绝不完整内容；本批不自动复制主模型的恢复政策给辅助调用。
- `FinishGate` 只处理被接受的响应和完成提议。不要简单把所有“没有提议”的返回都改为自动继续，这会破坏普通聊天和必要条件等待。

验收：合成 Earth 形态的 length + 空正文 + 无工具响应，下一次返回合法工具；断言工具恰好执行一次、两次费用入账、没有进入 waiting_user。再覆盖连续失败、暂停发生在恢复间隙、有效工具与破损工具混合、拒绝、重启恢复计数、正常聊天。

## 4. B：并行领域登记可靠化

### 4.1 原子登记资产

`domain/assets.py` 当前先 SELECT 再 INSERT，并在唯一键冲突时 rollback 调用方事务。改成 PostgreSQL 原子 upsert，继续使用 `uq_assets_endpoint_identity`，不移除 method 或 identity_key 去重维度。

核心形态如下，省略字段组装：

```python
stmt = pg_insert(AssetRow).values(**insert_values)
stmt = stmt.on_conflict_do_update(
    constraint="uq_assets_endpoint_identity",
    set_={
        "last_seen_at": func.greatest(AssetRow.last_seen_at, stmt.excluded.last_seen_at),
        "source_artifact_id": func.coalesce(
            AssetRow.source_artifact_id, stmt.excluded.source_artifact_id
        ),
    },
).returning(AssetRow)
```

保留已有 first_seen_at、来源、备注和其他领域内容，不用空值覆盖。返回 ORM 行时刷新 identity map。继续返回 `(row, created)`：可用本次预生成资产 UUID 与返回 UUID 比较判断是否插入；不要恒定返回 False 或依赖未约定的 PostgreSQL 内部字段。

资产 ID 与操作 ID 是不同概念。操作 ID 仍由 OperationRegistry 使用稳定调用身份生成。业务函数不自行 rollback 整个外层事务；失败由事务拥有者处理。

### 4.2 用真实采集上下文关联证据

现有 `record_lead`、`record_observation`、`propose_finding` 以 URL 登记资产时使用默认 GET。本版增加可选 `source_operation_id`，优先读取该操作真实方法、身份和入口；未提供时，可从明确且唯一的证据来源操作推导。

存在多个来源、多个方法或身份时保留证据引用集合，不任意选最后一次操作作为全部来源。只提供 URL 而缺少采集方法时不能伪装成已验证 GET 证据；模型可提供 method，或仅关联观察而暂不建立精确端点资产。

自动补全只填可唯一确定的元数据。程序不自动把响应文字改写成漏洞、不自动把 candidate 提升为 confirmed。

### 4.3 重放和并发测试

审计三个登记工具的 `OperationRegistry + result_snapshot` 完整性，保留已有正确路径。对于同一 run/tool_call_id，领域记录与 result_snapshot 同一事务提交，重放返回原结果；显式重测产生新的逻辑操作身份。

使用真实 PostgreSQL、两个独立连接和同步屏障：两个工具同时登记同一入口，两个业务操作均成功，资产只有一行；一个调用失败不能撤销另一个已经提交的观察。另测不同方法、不同身份不合并，以及提交后图写入前崩溃再恢复不产生重复记录。

## 5. C：焦点自动承接与轻量判断

### 5.1 图状态只增加一个小判断对象

`current_focus` 继续表示正在处理什么；增加 `source = explicit | plan_inferred`，自动推导的 reason 只写“由唯一进行中步骤承接”，不伪造模型判断。

新增一个 `work_assessment`，表示**最近一次明确判断**；历史由消息、领域来源和阶段报告保存，不在 checkpoint 无限追加日志。

```python
class WorkAssessment(TypedDict):
    step_id: str | None
    lead_id: str | None
    changed: str                 # 这次结果改变了什么
    next_action: str             # 下一步准备消除哪个不确定性
    evidence_refs: list[str]     # 已有来源引用；纯计划调整可为空
    uncertainty: str | None      # 未测到、条件不成立、假设受反证等
    source_tool_call_id: str     # 程序注入
    revision: int               # 程序维护
```

`changed` 和 `next_action` 是模型核心输入，其余能推导则推导。建议单字段最多 500 字、引用最多 8 个，作为工具负载初值；不要求填写分数、整套实验表格或隐藏推理。

该对象保存解释而不是漏洞事实；只读黑板和报告投影它。`work_summary` 继续承担压缩历史摘要，不能反过来覆盖较新的 work_assessment。两者都标来源范围，避免压缩前的旧下一步冒充当前下一步。

### 5.2 焦点解析规则

新增纯函数 `resolve_focus(plan, previous_focus, explicit_focus)`，由 `update_plan` 和 WorkingContext 共用：

1. 模型明确选择有效步骤/线索时采用，并检查会话归属。
2. 没有明确选择，恰好一个 in_progress 步骤时承接该步骤；lead_id 只取该步骤已有绑定，不从最近工具名猜测。
3. 多个进行中步骤且旧焦点仍有效时保留；无有效旧焦点时保留 unresolved，不任意选第一项，并给一次简短提示。
4. 步骤 done、dropped、blocked 或被移除时，不继续沿用为当前执行焦点。保存其 resume_note 或线索状态，转入其他有效焦点或 unresolved。
5. 没有计划的初始探索仍可执行。不得为了让关联率达到 100% 而强造步骤。

WorkingContext 在装配前按上述规则解析，并用同一解析结果同时构造黑板、返回图更新。没有语义变化时不反复递增 revision。

### 5.3 扩展既有工具，避免多套登记入口

扩展 `update_plan`：

```python
async def update_plan(
    runtime,
    tool_call_id,
    steps_json: str | None = None,
    rationale: str | None = None,
    focus_json: str | None = None,
    assessment_json: str | None = None,
) -> Command:
    ...
```

- `steps_json=None` 表示保留计划；显式 `[]` 才是清空，清空不能留下失效焦点。
- 可以仅更新当前判断，不必重写整张计划。判断更新只增 assessment revision，不能把它冒充计划结构变化。
- 计划整体替换语义不变，旧调用继续可用。
- `record_lead` 已支持 `observation_summary`、证据、下一步及合并，一次登记线索和关键观察；直接复用，不新增同义 `record_work` 工具。
- 需要长期保留的未决方向进入 Lead；最近判断对象不能代替线索库。只是在同一方向内推进而不改变计划时，一次 `record_lead` 更新就够，不强制同时调用 update_plan。

模型只在以下转折补充短记录：获得实质性证据、关键假设被否定、验证条件不成立、需要换方向、短暂检查新线索后返回。连续普通请求无需额外模型总结调用。

### 5.4 并行与暂停语义

工具调用继承的是**发出这一批工具时已确定的焦点**，不是某个并行工具写到一半的新状态。`tools/execution_context.py` 保留 ContextVar，记录 plan_revision、focus_source 和已知的 step/lead ID。

若模型把 update_plan 与执行工具放在同一批，执行记录明确标为原快照关联，不能显示成已采用新计划。方法提示说明：切换主线后需要精确关联的新动作放下一轮；同批独立探测继续允许。多个 update_plan 同批会竞争图状态，在响应可接受性检查中给格式反馈，不能靠 last-write-wins。

`record_lead` 不直接写 current_focus，避免多个并行登记都返回 Command 修改同一单值状态。需要转向某条新线索时，在取得真实 lead_id 后通过 update_plan 选择它。

短暂验证新线索时，原步骤保留 resume_note，焦点保存 return_to_step_id；窗口以“先验证关键假设的一小步”为指导，不设任务时间截止。得到新证据再调整顺序，没有依据则记录并返回原主线。

当前图 schema 已是 2。实施本版状态语义时升级为 3，增加显式 2→3 迁移；1→2→3 顺序兼容。迁移保留消息 ID、操作 ID、计划和待执行节点，遵守现有 pending writes 检查。不能把所有旧步骤自动标完成，也不能重写历史操作的焦点来美化关联率。

## 6. D：结构化请求构造

### 6.1 最小接口

保留现有 `body`、`json_body_json`；为 http_request 增加 `form_fields_json`、`multipart_parts_json`。四种请求体互斥。raw body 保留，不自动修正用户有意构造的非标准协议内容。

建议契约：

```python
class FormField(BaseModel):
    name: str
    value: str

class MultipartPart(BaseModel):
    name: str
    value: str
    filename: str | None = None
    content_type: str | None = None
```

表单使用数组，支持重复字段名和原顺序。第一批只实现有界 UTF-8 文本字段和文本文件 part，足以消除手写 boundary/换行的机械负担；二进制和大文件随后通过既有工件/工作区文件引用接入，本批不让模型生成大段 base64。

请求体形态在 `contracts/http.py` 定义为协议真源，执行端消费生成/同步后的协议模型。不能让控制端和 Executor 各自维护不同字段与约束；加入契约一致性测试，沿用现有包依赖边界，不让 Executor 导入控制端业务模块。

### 6.2 字节构造和执行记录

- form 统一按规定编码；multipart 由执行端编码器/成熟 HTTP 库产生 CRLF、boundary、Content-Disposition 和最终字节长度。正常模式拒绝相冲突的手工 multipart boundary，返回明确构造错误。
- 请求的逻辑参数摘要与 operation_id 保持稳定。multipart 随机 boundary 不能混进逻辑请求去重键；真实生成的字节 hash 另记。Executor 接受任务后为其固定编码结果，重放从台账返回而不重新发送。
- 原有 CSRF 自动补全接入结构化字段；不能把 multipart 又退化成手拼字符串。重复 token 字段的替换/追加策略明确并测试。
- 维持身份隔离、范围检查、重定向策略和取消语义。303 改 GET 后移除请求体及对应实体头；307/308 重发同一编码体，逐跳仍作已有目标检查。
- 工具结果增加 `request_encoding`、`request_body_bytes`、`request_body_sha256`、`request_metadata_artifact_id`，不直接回显敏感值。
- 错误按 `construction`、`transport`、`response` 标注；存在歧义的网络错误另标 `delivery_state=unknown`。网络失败不能一概说“没有提交到目标”。
- `request_encoded=true` 只证明本地构造成立，不证明服务端解析成功或测试条件充分。服务端条件仍由可见响应和必要的最小验证判断。

Executor capabilities 新增支持的 body modes，旧端默认为已有模式。控制端在调用前检查；旧端不支持时明确返回能力不足，不悄悄降级构造。升级应先执行端、后控制端；两端版本写入实验配置。

验收：本地回显服务独立解析请求，检查普通字段、重复字段、中文、文本文件、CRLF、Content-Type 一致性；再测 raw 原样保留、CSRF、身份、重定向和重放。不向 S2-045 或其他真实靶场发送请求。

## 7. E：进展提示、旧线索与优先级

### 7.1 分离三个概念

| 信号 | 数据来源 | 用法 |
|---|---|---|
| execution activity | 操作数量、状态、请求摘要、运行中 job | 告诉用户在做事；不算知识增量 |
| knowledge changes | 观察/线索/发现的有效版本变化、明确判断及其来源 | 候选的信息变化信号；仍不宣称已理解语义 |
| result similarity | 同一问题下结果状态、规范化响应类别、重复错误等 | 触发温和提醒；不是无价值判决 |

`progress.py` 不再把新的 request_digest 直接放入知识进展签名。新增观察数量也不等价于新知识；对完全相同来源和内容的重复写入不重置提醒。模型仅改写措辞、不增加来源的判断更新标为 assessment_changed，不宣称得到新证据。

结果相似不能只用完整 body hash：时间戳等动态内容会扰动。首版采用保守的状态/类型/已知错误归类加来源引用，标注置信不足；不新增独立语义判别模型。不确定是否相似就保持 unknown。

起始调参值：同一焦点下连续 3 个已完成回合出现相似结果且没有明确判断变化时给一次提示；8 个回合内不重复。它只影响提醒节奏，不阻止合法请求、不阻止人工要求的复查、不自动结束任务。更新后的字段建议使用 neutral 名称 `similar_result_streak`，不继续把它展示成已证实的“无进展”。

提示固定为具体短句：

> 最近几次结果相似。它们否定了哪个假设？如果测试条件尚未成立，先做一个能区分条件问题与路径不通的最小验证；仍有证据支撑就继续，核心假设被否定再换方向。

### 7.2 旧线索按需检索与合并

沿用当前焦点、少量候选和轮转旧线索的只读黑板。新增明确触发：获得新的身份/访问条件、关键假设变化、主线受阻或主线完成；正常轮转作为兜底，不能只依赖字符串精确包含整个目标句。

优先用已有 step/lead/asset/identity 关系检索，再用有限关键词辅助。不在本版新建向量库或图谱。召回结果进入实际发送上下文后才推进 review_cursor；仅查询到、但因预算被省略的线索不能算已展示。

方法提示补充：验证关键假设和下一步相同的未完成线索可以合并推进，但不同目标条件、身份与证据范围要保留；采用已有 merge_into_id 保存来源和重定向，不能把“方法相似”直接等价为“同一漏洞”。需要相同方法但独立结论时，共用计划步骤即可，不强制合并领域对象。

优先级保持定性分层：当前重要问题 → 有证据且能以小动作区分的新线索 → 可推进的旧线索 → 泛化扩面。排序由模型结合目标判断；程序提供可见候选与恢复位置，不引入难以校准的总分调度器。

长期记忆继续只保存方法、失败条件和验证经验，跨目标事实重新核实。本版不扩大长期记忆模型调用频率，也不引入目标专属成功路径。

## 8. F：报告和工作记录与压缩解耦

### 8.1 报告内容来源

| 报告字段 | 权威来源 |
|---|---|
| 做到了什么 | 业务表观察/候选/确认记录及证据引用 |
| 当前在处理什么 | checkpoint current_focus + plan |
| 为什么调整、下一步 | 当前适用的 work_assessment，或相关 Lead.next_action |
| 过去发生了什么 | 带来源范围的 work_summary、已有阶段报告 |
| 暂停/失败原因及未完成操作 | Run 和 Executor 台账 |

没有模型判断时明确写“尚未记录判断”，并展示真实操作摘要、计划与待处理状态；不能由模板自动补成“环境无漏洞”或“已充分覆盖”。不把隐藏推理当成解释来源。

新的阶段签名包含有效领域版本、完成步骤和 work_assessment revision；普通模型流和新工件 hash 不触发报告。首次看到已有实质内容应生成快照，不能一律设基线后跳过。

`stage_key` 沿用现有机制，继续使用现有生成锁和失败重试。新增内容不要求每回合额外摘要模型调用，报告优先从已有结构化数据渲染。

### 8.2 暂停、故障与一致性

Runner 在暂停、故障和合法阶段结束时捕获一个明确的阶段版本，写入 `run_id`、checkpoint 标识、图读取时间、领域快照时间、最新已覆盖事件/操作位置和 trigger。图与业务数据库不在同一事务，继续如实标 `atomic_with_domain=false`。

报告明确区分“生成时间”和“覆盖到的位置”；旧快照不是自动覆盖所有最新活动。跨来源读取出现较新领域记录时明确标示，避免伪装成同一时刻的原子截图。

生成失败保留已有报告，不回滚探索，不新增另一条 Agent 循环。原 stage_key 重试不得用新的状态覆盖旧快照；若确有新内容，产生新 key 和新版本。

前端只增加必要投影：当前步骤、最近判断、下一步、阶段报告覆盖时间、模型响应异常/恢复状态。协议变更通过现有 OpenAPI 生成，不手写第二份枚举。人工暂停和模型故障的文案必须区分。

## 9. 工程验收清单

| 测试位置（计划） | 必须实际证明 |
|---|---|
| `tests/unit/test_response_quality.py` | 空结构、空正文、length、拒绝、invalid_tool_calls、正常内容块的分类 |
| `tests/integration/test_response_recovery.py` | 通过真实 create_agent 装配验证恢复、用量、取消；坏响应工具零派发 |
| `tests/unit/test_context_budget.py` | 不同恢复额度下最终 wire 请求和预算一致；计入供应商回传字段 |
| `tests/integration/test_asset_concurrency.py` | 真实双事务并行登记；正确去重；外层记录不被误回滚 |
| `tests/integration/test_work_focus.py` | 唯一进行中自动承接、多进行中不乱猜、旧焦点完成清理、同批快照关联 |
| `tests/integration/test_state_v3_migration.py` | schema 1/2 迁移，消息和 pending writes 保留，历史操作不重放 |
| `tests/integration/test_structured_http.py` | 本地独立解析真实字节，覆盖重定向/身份/CSRF/能力版本 |
| 现有 `test_progress.py` 与新场景 | 只换请求不宣称新知识；相似结果提醒不终止；有依据复查可继续 |
| 现有 `test_stage_reports.py` 与新场景 | 无压缩仍有判断；首次有效快照；同 key 重试；故障报告不冒充完成 |
| 现有暂停、控制版本、恢复测试 | 恢复期间用户暂停/改目标仍生效；旧工具不被重新派发 |

每个局部批次先运行相关测试；集成完成运行 AGENTS.md 规定的 ruff、pyright、编码、unit/integration 检查，外加实际触及的 recovery/Executor 测试。不能只跑控制端测试就宣布请求构造完成。

检查通过仅代表工程行为满足断言。真实安全测试有效性单独验收，不能用测试数量或工具数量替代。

## 10. 用户执行的三组真实复测

### 10.1 固定实验条件

每轮新建独立会话，记录控制端/Executor commit、源码脏树指纹、模型和 profile 版本、提示词版本、正常/恢复输出额度、输入预算、靶场重置情况、任务原文和人工介入。继续使用 S2-045、Tomcat8、Earth，不读取公开解题路径或向提示词注入答案。

可比较条件下保持原目标指令；无法恢复相同环境则标注差异。先完成一轮 V2 复测；需要判断稳定性时再由用户做同配置重复轮次，不因一次通过宣称普遍有效。

为避免长期记忆干扰，第一组对比关闭跨会话经验读取和写入，或为每轮使用同一冻结的经验快照并关闭写入；再单独实验经验启用效果。已有 V1 样本缺少部分实验标签，作为问题基线，不能包装成严格随机对照。

### 10.2 分目标观察

| 环境 | 本轮主要问题 | 复测应看什么 | 失败判据 |
|---|---|---|---|
| S2-045 | 测试条件与阴性解释 | 请求构造有可复核元数据；未观测到结果时先判断条件是否成立；关键判断留下来源 | 仅凭空回显就断言修复/不存在；重要方向无记录而被遗忘 |
| Tomcat8 | 已有成果后的方向和登记可靠性 | 重要成果完整落库；说明继续验证影响或扩面的理由；并行登记不分散注意力 | 再次需要删 URL 绕过内部错误；把可见访问能力升级成未经证实的后续影响 |
| Earth | 空响应停摆及后续连续性 | 能处理截断并继续；无法恢复时明确故障；可查看暂停前进度与恢复点 | 无提议静默 waiting_user；无限重试；旧工具重放；费用漏计 |

Earth 的合成回归只证明确定断点被修复；真实复测仍要看恢复后的行动是否产生有用结果，不能以“多跑了几步”验收探索能力。

### 10.3 复盘输出与采集口径

继续导出脱敏 `REPORT.md`、`review-evidence.json`、SHA256 清单。安全导出脚本只允许字段白名单；原始请求体、凭据、Cookie、推理正文和靶场答案保留在本机受限诊断材料，不进入研究报告。

记录响应质量、各次调用用量、工件引用、计划/焦点版本、判断更新、登记失败、召回实际展示、结束原因。对“有效信息增益”和“结论是否证据相称”进行人工复核，不自动把计数转换为成功率。

核心验收问题只有四个：

1. 运行故障能否明确处理，而不是被误认为结束？
2. 重要结果是否进入可检索记录，后续行动能否接续？
3. 失败是否帮助区分条件、工具和假设问题，而不是只换参数？
4. 最终交付是否忠实于证据，并留下未完成问题和恢复位置？

## 11. 完成边界与后续讨论

本版完成标准是上述六个改动形成可验证的闭环，而不是增加一个庞大的任务决策框架。三次真实复测结果出来后，再讨论是否需要更强的优先级策略、长期记忆检索或自主收敛规则。

本方案没有重新研究 ARTEX 新版本，不把本版机制标为 ARTEX 已验证能力，也不直接复用外部代码；未来引用外部实现时另固定 commit、源码位置与许可证。

实施前再次检查工作树，只改本批对应文件；现存 `.workbuddy` 修改和实验复核材料保持独立。所有新增/修改文件使用 UTF-8 无 BOM。此文档自身是落地设计，不构成产品能力已实现或靶场验收通过的声明。

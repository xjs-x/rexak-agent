# 第三版代码落地方案：弱点关系驱动的持续探索

日期：2026-10-14。基线：`730b7a9`，分支 `codex/implementation-v1`。

本文是待实施的编码方案。本轮仅新增此方案文档，未修改实现、数据库、部署或实验数据。
工作目录固定为 `C:/xjs/rexak-agent`；`C:/xjs/xak-agent` 不在修改范围。

## 1. 已确定的产品行为

1. 第一优先级 P0：Finding 升级为弱点，同时实现清单与链条共同推进。两者共同交付。
2. 次高优先级 P1：独立修正结束判定，可以与 P0 的业务实现并行推进。
3. 最后 P2：增强同会话内的条件变化召回，重点是新弱点、身份变化、主线受阻与阻塞条件变化。
4. 默认继续追踪仍有可执行、预期能增加信息的线索，不再用“不是当前目标必需”作为自主结束的豁免。
5. 暂缓改变执行顺序，受阻表示当前缺少条件；二者均不等于已解决。
6. 保留单 Agent、官方 `create_agent`、现有供应商适配、按指令范围、暂停继续及执行端幂等性。
7. 不新增任务总时间、总费用、总工具调用次数上限。单请求、单操作和上下文资源边界继续有效。
8. 不增加逐轮评审模型。关系判断由主模型完成，程序负责记录、检索、版本一致性和遗漏检查。

用户描述的 Earth“已有 Finding 没有被后续消费”和 Tomcat8-1“已有线索被结束处理放过”作为回归场景来源。
具体运行 ID、证据位置和事件顺序，需在读取第二版日志后固定；当前不把描述当作已经完成逐条日志复核。

## 2. 当前代码缺口与修改依据

| 当前实现 | 已复核的缺口 | 落地改动 |
|---|---|---|
| `agent/state.py::PlanStep` | 有单个 lead_id、完成条件和证据，没有能够表达来源弱点与前置条件的领域关系 | 计划关联多条线索，前置条件由线索关系投影 |
| `domain/findings.py::FindingService` | 以候选发现、验证和报告为核心，没有条件描述及后续用途判断 | 扩展现有 Finding 记录和服务 |
| `tools/history.py::query_history` | 检索类型中没有 Finding | 增加 weakness 检索、关系邻居和指定对象详情 |
| `agent/working_context.py` | 主要组装未完成 Lead；没有新弱点的消费入口 | 加入待分析弱点、关联线索和条件变化 |
| `runtime/stopping.py::_open_relevant_leads` | 检查集合来自模型声明；blocked 被跳过；处置标签和非空理由可放行 | 由数据库补齐对象，改为检查实际剩余推进可能性 |
| `domain/leads.py::set_status` | 文案要求恢复条件，实际只检查理由 | 分离状态和可推进性判断，维护具体缺失条件 |
| `domain/reports.py` | open_leads 排除了 blocked | 保留受阻与暂缓事项，并输出关系与限制 |
| `tests/integration/test_stage_reports.py` | 现有测试认可未声明相关性的高优先级线索不阻止结束 | 更新产品语义测试，不能仅追加不改变旧断言的新测试 |

`open_high_priority()` 当前没有被实际结束路径调用，不能只修改这个辅助方法就声称修复门禁。

## 3. 权威归属与数据模型

### 3.1 复用 Finding 表作为弱点实体

物理表继续使用 `findings`，保留 UUID、验证外键、历史报告和原 API。产品概念与新工具使用“弱点”。
不另建一张与 Finding 同步写入的 Weakness 主表，不复制事实。

`FindingRow` 增加四个字段：

| 字段 | 类型 / 默认值 | 语义 |
|---|---|---|
| `weakness_kind` | 字符串，`unclassified` | `vulnerability / enabling_condition / unclassified`，区分独立缺陷与可供后续利用的条件 |
| `applicability` | JSONB，`{}` | 适用资产、身份、已知前置条件和限制；复用已有 asset_id、identity_id，不建立另一份身份库 |
| `provided_conditions` | JSONB，`[]` | 有证据引用的条件陈述，包含稳定 key、简短 statement、evidence_refs、limitations |
| `connection_review` | JSONB，可空 | 模型对当前版本后续用途的短判断：依据指纹、关联线索 ID、暂未发现连接的说明、来源工具调用 |

`provided_conditions` 的证据引用说明判断依据，不自动证明陈述成立；候选弱点依然是候选。
父弱点已经 confirmed，也不能使新增条件和组合关系自动 confirmed。
现有 VerificationService 和数据库 confirmed 约束保留，不要求为每个条件新增一轮独立复测。

已确认记录的原有核心事实不原地扩大：发现新的事实或扩大适用范围时创建候选记录，再建立关系。
追加条件说明须单独显示其依据，不能借用旧验证结论扩大结论。

稳定 condition key 由领域服务在幂等事务内分配并回传；工具重放返回原 key。
更新已有条件沿用 key，删除被引用条件前必须处理引用；普通修订保留引用并使旧判断过期。

`revision` 对实质内容变化递增。仅更新“已分析后续用途”的元数据不递增知识版本，防止判断刚写入就使自己过期。
同内容重复更新不增加知识进展；文字变化也不能单独证明获得了新信息。

### 3.2 线索承载待验证连接

`LeadRow` 保留 hypothesis、next_action、priority、status、证据和合并机制，增加：

| 字段 | 类型 / 默认值 | 语义 |
|---|---|---|
| `required_conditions` | JSONB，`[]` | 条件 key、描述，以及可选的已有对象引用或预期产出该条件的 lead_id |
| `actionability_assessment` | JSONB，可空 | `ready / needs_condition / no_new_action / needs_review`，附依据指纹、已有操作/证据引用和短说明 |

条件引用可指向已有弱点条件、身份、资产，或另一条线索预期获得的结果；没有对象时保留文本条件。
引用另一条线索的预期结果只表示依赖意图，不伪造一个已经存在的弱点。
同一条件可有多种候选满足方式，首版不实现复杂的布尔规则引擎，由模型评估替代关系并保留依据。

`actionability_assessment` 描述当前能否推进；原 status 描述工作状态。
例如 deferred + ready 表示“可以做但当前排在后面”，仍然阻止自主结束。
blocked 但没有明确缺失条件，归为 needs_review，不能直接放行。

判断的依据指纹覆盖线索假设、下一动作、条件、来源弱点的有效版本，以及相关身份/范围状态；
不把判断字段本身、纯排序变化和展示时间放入指纹，避免自我失效。
条件来源变化后，旧判断成为 needs_review；不直接把它判成可执行或已解决。

### 3.3 新增一张关系表

新增 `LeadWeaknessLinkRow`，表名 `lead_weakness_links`：

```text
id, session_id, lead_id, finding_id
role: basis | result
condition_key: 可空
requirement_key: 可空
source_weakness_revision
relation_state: proposed | evidence_linked | refuted
evidence_refs
source_operation_id
revision, created_at, updated_at
```

- `basis`：这条线索从哪个弱点/条件获得依据。
- `result`：这条线索的验证实际产生或更新了哪个弱点。
- `evidence_linked` 表示关系已有引用依据，不等于整条组合路径已确认。
- 链条读模型由“弱点 → basis 关系 → 线索 → result 关系 → 弱点”组成。
- 计划步骤不重复存储另一份弱点关系；它引用 Lead ID，按需读取上述关系。

约束与索引：

1. lead、finding 必须属于同一 session；服务检查与数据库复合约束一致。
2. 对 `(lead_id, finding_id, role, condition_key, requirement_key)` 建唯一身份；空 key 统一表示，避免 NULL 导致重复。
3. 对 session + lead、session + finding、finding + condition_key 建检索索引。
4. 删除端点采用 RESTRICT 或保留历史的业务操作，禁止静默级联抹掉关系证据。
5. 旧版本来源不自动升级为新版本判断；保留原引用，重新评估后再更新关系版本。
6. 关系可有回路；回路本身不能作为证据。线索别名合并仍禁止形成循环。

### 3.4 归属保持清晰

| 信息 | 权威位置 |
|---|---|
| 弱点、条件、线索、关系、用途与可推进性判断 | 业务表 |
| 当前计划、当前焦点、实际呈现游标、阶段结束复查进度 | LangGraph checkpoint |
| HTTP、浏览器、命令等真实操作状态 | Executor / 控制端操作台账 |
| 黑板、关系面板、结束复查列表 | 上述权威资料的只读投影 |

不增加通用任务状态表、独立 Agent 调度器或第二套可写黑板。

## 4. 工具与领域服务改造

### 4.1 新的主写入口 `record_weakness`

建议签名的业务部分：

```python
record_weakness(
    weakness_id=None,
    expected_revision=None,
    title=None,
    description=None,
    category=None,
    asset_id=None,
    identity_id=None,
    impact=None,
    weakness_kind=None,
    applicability_json=None,
    provided_conditions_json=None,
    evidence_refs_json=None,
    connection_review_json=None,
)
```

- 首次创建与后续补充走同一个领域实现；写入幂等沿用 OperationRegistry 和原 tool_call_id。
- 只允许创建 candidate，不接受模型直接传 confirmed。
- `None` 表示保留已有值；显式空值的清空行为按字段定义，不能把省略误当清空。
- 返回 weakness_id、revision、条件 key、实际新增/变化项、后续用途是否需要重评；不返回长原始正文。
- 指定 expected_revision，过期写入返回明确冲突，不覆盖新的证据和条件。
- 用途判断可与条件补充一同提交，但须针对本次写入后的版本保存。
- “已产生后续线索”的判断必须引用真实存在的关联；“暂未找到后续用途”允许保留，但需要短说明，不虚构连接。

原 `propose_finding` 保留兼容入口，共用内部实现。第一版不能移除旧 checkpoint 可能调用的工具名。
保持旧工具的操作身份和请求摘要兼容，不能因别名转换使旧调用在重放时变成新操作。

### 4.2 扩展 `record_lead`

新增可选参数：

```python
source_weaknesses_json=None
result_weaknesses_json=None
required_conditions_json=None
actionability_json=None
expected_revision=None
```

关系项携带 weakness_id、condition_key、来源 revision、可选 requirement_key 和证据引用。
线索变更与关系写入在同一事务中完成，失败整体撤销。
不接受来自其他会话的对象，也不能只凭文字创建“已验证的组合”。

合并时执行：解析主线索 → 检查循环 → 合并关系并去重 → 保留各自适用条件与证据 → 使受影响判断重评。
验证方法相同而结论独立的线索，不自动合并；让同一个计划步骤引用多条 Lead 即可。

### 4.3 扩展 `update_plan`

PlanStep 增加：

```python
kind: "coverage" | "followup" | "mixed"
lead_ids: list[str]
```

保留原 step_id、status、done_when、resume_note、evidence_refs。
保留旧 lead_id 兼容投影；读取旧步骤时映射到 lead_ids，新写入时两者不得冲突。
焦点仍然允许一个主 lead_id；不能因为步骤同时服务三条线索，就虚构三个当前焦点。

步骤所需条件和来源弱点从 Lead/关系表投影，不另存一套可写 depends_on 图。
尚未产生的条件可通过 required_conditions 的 producer lead 引用表达先后关系。
由模型调整计划顺序；程序提示条件缺失及可能受影响的步骤，不直接自动执行新工具。

### 4.4 扩展检索

`search_history` 增加 `kind="weakness"`，支持 weakness_id、lead_id、关系邻居和状态筛选。
保留既有关键词查询与稳定分页。新增按 ID 读取详情的路径，不能只依赖关键词命中。

首版查询顺序：明确 ID/条件关系 → 同资产/身份 → 受影响的前置条件 → 关键词兜底。
模型只拿简短结果和来源引用，需要原始证据时继续使用 read_artifact。
不引入图数据库或向量检索依赖。

## 5. P0 核心：让弱点真正进入下一次决策

### 5.1 执行链路

```text
执行工具取得结果
  → record_observation / record_weakness 保存依据
  → 领域写入返回弱点 ID、版本、变化条件
  → 下一模型回合，WorkingContext 读取待分析弱点及已有关系
  → 模型判断已有路线、新路线或暂未发现用途
  → record_lead 写连接/条件，update_plan 安排推进
  → 官方 create_agent 执行下一批工具
  → 结果回写线索与弱点关系
```

新弱点与依赖其返回 ID 的线索写入放在不同工具批次。若使用已有弱点，也须校验来源版本。
同批并行仍继承调用发出时的计划和焦点快照，沿用 V2 的执行关联语义。

### 5.2 修改现有 WorkingContext，而非增加一个 Agent

在现有投影中增加有界区块：

```text
changed_weaknesses: 本轮尚待分析的弱点摘要
related_leads: 已显式关联、可能受本次变化影响的线索
condition_changes: 哪些条件增加、修订或失去依据
current_route: 当前主线的一跳关系、待解决条件和证据引用
pending_connection_review_count: 尚未判断后续用途的弱点数量
```

候选优先级采用定性提示：当前主线相关、可能解除其他路线阻塞、有明确区分性验证、其他待分析项。
沿用现有四维分数作为参考；“影响了多少条线索”作为关系信息展示，不直接变成新的硬分数调度器。

没有显式关系的新弱点，也必须有进入待分析列表的机会。不能只有“已关联对象”才能检索到，否则无法建立第一条关系。
旧 Finding 的条件字段迁移后为空，按页进入“待整理弱点”，由模型依据旧证据补充；不能自动编造历史关系。

### 5.3 区分保存、呈现、判断与执行

验收及诊断分别记录四件事：

1. **保存**：业务记录已提交。
2. **呈现**：本次请求实际携带了对象及版本，由 context manifest 证明。
3. **判断**：模型写了关联线索或“暂未找到用途”的短判断，带对应来源版本。
4. **执行**：真实操作的 execution_context 指向相关步骤和线索。

不能把“加入黑板候选列表”记成已呈现，也不能把“建立了一条关系”直接记成已经执行。
不强迫每个弱点生成任务；允许有依据地记录暂未发现可推进连接。
但尚未做用途判断的实质弱点，在自主结束复查时仍然是待处理项。

### 5.4 上下文预算

- 关系区块进入现有实际请求预算计算，不建立脱离模型请求的另一套 token 口径。
- 初始每次呈现最多 2 条变化弱点、当前一跳关系和最多 3 条关联线索；这些是上下文资源初值，不是总探索数量上限。
- 按整条对象裁剪，保留 ID、版本、关键条件及下一页引用。当前焦点和未决关键条件优先。
- 未进入最终 payload 的条目不能推进呈现游标。呈现过但没有判断，仍保留待判断状态。
- 供应商失败、恢复尝试、压缩和取消都不能伪造完成判断。
- 长证据与大量关系保存在原领域记录和工件中，通过分页检索读取。

### 5.5 提示词与方法资料

系统提示只增加短原则：弱点是后续决策来源；新条件出现时检查可连接路线；未知组合保持假设；有可推进方向时默认继续。
详细方法新增 `agent/prompts/methods/weakness-chains.md`，登记到方法资料目录，按需加载。
提示版本预计 5→6，方法 bundle 版本按实际发布递增。
不用任务专属提示，不复制 Earth/Tomcat 解法，不要求输出隐藏推理。

## 6. P1：独立修复结束判定

### 6.1 程序决定复查集合

将 `_open_relevant_leads()` 替换为按 session 枚举规范主线索的 review inventory。
模型的 relevant_lead_ids 仅作为展示偏好，不能作为排除其他对象的依据。

纳入：所有未完成主线索、暂缓/受阻线索、合并后的未完成主对象、未完成计划事项，以及已登记的覆盖缺口。
P0 接入后增加待分析弱点和失效的条件关系判断。
不按 priority 阈值裁掉低分线索，不依赖 `.limit(200)` 作为完整集合。

已确认或已反证的线索需有结果来源与适用条件；确认一条线索后产生的新弱点仍走后续用途判断。
关闭某个假设不代表其相关弱点和其他分支都关闭。

### 6.2 有限分页复查

服务器分页扫描完整集合，每次只把少量待判断对象交回模型。
模型在现有 record_lead 中写 actionability_assessment，复用已有证据与操作，不强制重新发请求。
扫描状态放 checkpoint；对象判断放业务表，不另建一个可写的任务清单。

判断过期检查采用每个对象的依赖指纹。提交判断时检查旧版本，在事务内绑定写入后的有效版本。
最终放行前再次检查全体对象，防止新记录落在旧分页游标之前而被遗漏。
新增或变化对象需要重评；没有变化的对象不要求重新填写理由。

### 6.3 决策顺序

```python
if pending_user_commands:
    return continue_work
if invalid_confirmed_evidence_or_bad_references:
    return continue_with_specific_feedback
if unreviewed_or_stale_items:
    return continue_with_review_page
if ready_leads_or_feasible_coverage_work:
    return continue_work
if unresolved_tool_jobs:
    return wait_tool
if all_remaining_useful_routes_require_external_conditions:
    return wait_user
if no_supported_next_action_remains:
    return complete_current_stage_with_limitations
return continue_with_specific_feedback
```

这里 `continue_work` 表示继续官方图，由模型选择动作；不是运行一个新的调度循环。
执行状态未知的操作仍需核对；可以推进独立工作，但不能重发状态未知的同一操作来假装重试。
不能因为某一条路线缺账号，就在仍有其他可推进路线时整体进入 waiting_user。
用户主动暂停、取消的优先级和现有控制版本机制保持有效，不受自主结束复查拖延。

### 6.4 明确关闭原来的豁免入口

- `blocked`、`deferred` 不在自动终态集合中。
- `deferred / outside_goal / not_required + reason` 不再直接通过检查。
- 旧 completion 参数可保留用于兼容读取，但其内容只作为说明，不作为放行凭据。
- `goal_answered` 保留兼容枚举和回答说明，不能跳过剩余线索与覆盖检查。
- 程序不因为“查到一个漏洞”或“主目标已回答”自动停止。
- 受阻覆盖项必须保留在复查和报告中，不能通过从 not_checked 改成 blocked 消除缺口。

`no_new_action` 不是新的免检标签。它必须绑定本次依赖版本，引用已有尝试或证据，说明尚有哪些未知、为什么当前没有能区分它们的新动作。
若仍保留明确下一动作、未解决的可处理条件、来源已更新，或关系仍待判断，程序拒绝据此结束。

程序只能检查遗漏、版本和结构矛盾，不能证明自然语言理由一定正确，也不能证明模型已经想到所有方法。
模型是否过早判断“无新动作”必须通过真实实验评估；不以更多必填文字冒充推理质量提升。

## 7. P2：条件变化触发的同会话召回

P0 已覆盖变化弱点和明确关联线索；P2 扩大到离开上下文、尚未建立显式关系的旧线索。

| 触发 | 查找范围 | 后续行为 |
|---|---|---|
| 新弱点或条件修订 | 条件引用、同资产/身份、关键词候选 | 提示可能获得新条件，模型重新判断 |
| 身份或 auth_revision 变化 | 依赖该身份的条件与历史受阻线索 | 旧判断过期，检查条件是否真的改善 |
| 弱点被反证或适用范围收窄 | 下游 basis 关系及相关计划步骤 | 标记待复核，不级联判定全部线索 disproved |
| 主线受阻或暂时没有有效动作 | 其他未完成路线及稳定轮转项 | 重新排序，不把它们自动判为已解阻 |
| 有具体结果的后台任务完成 | 关联操作、线索与证据 | 结果核对后再调整判断 |

先依靠精确 ID 和条件引用，后以资产/身份和关键词生成候选。相似文字不能自动建立成立关系。
按“对象 + 实质版本 + 触发原因”去重，防止每个普通请求都召回同一批内容。
同会话是默认检索边界，不因本版本自动跨会话复用目标事实。

## 8. 文件级编码清单

下列新增文件名是实施目标，尚未创建。

| 文件 | 编码责任 |
|---|---|
| `contracts/weaknesses.py`（新增） | 条件、适用范围、关系、弱点读写协议 |
| `contracts/exploration.py`（新增） | 可推进性判断、关系分页、结束复查投影 |
| `contracts/plan.py`、`contracts/enums.py` | 步骤多线索关联、类型与兼容字段；唯一枚举真源 |
| `persistence/tables.py` | Finding/Lead 扩展、关系表、约束与索引 |
| `migrations/versions/<revision>_weakness_relations.py`（新增） | 增量业务迁移，从实施时真实 Alembic head 接续 |
| `domain/findings.py` | 弱点创建/补充、内容版本、用途判断、现有验证兼容 |
| `domain/leads.py` | 条件、可推进性、合并关系和状态语义 |
| `domain/weakness_relations.py`（新增） | 同会话关系写入、版本核对、邻居检索、影响范围查询 |
| `domain/exploration_review.py`（新增） | 完整复查集合、指纹与分页；不 import agent |
| `tools/task.py` | record_weakness、旧工具兼容、record_lead/update_plan 扩展 |
| `tools/history.py`、`tools/registry.py` | 弱点和关系检索、工具注册与旧 checkpoint 兼容 |
| `tools/execution_context.py` | 保留主 lead_id，同时附本步骤服务的 lead_ids 与来源弱点版本 |
| `agent/state.py`、`agent/state_migration.py` | schema 3→4、计划兼容、呈现及复查进度 |
| `agent/working_context.py`、`agent/middleware/working_context.py` | 变化弱点、当前路线和相关线索的只读投影 |
| `agent/middleware/context_builder.py` | 整对象预算裁剪、真实发送 manifest、来源版本 |
| `agent/middleware/progress.py` | 纳入有来源的新条件/关系；单纯 review 元数据不计知识进展 |
| `runtime/stopping.py`、`tools/completion.py`、`agent/middleware/finish_gate.py` | 新结束语义、分页反馈、兼容参数与等待选择 |
| `agent/prompts/system.py`、`methods.py`、`methods/weakness-chains.md` | 短原则和按需方法资料 |
| `domain/reports.py`、阶段报告相关 middleware | 弱点、链条已知/未知部分、受阻与暂缓事项；阶段签名包含有效关系版本 |
| `api/routes.py` | 弱点读接口、旧 findings 兼容、关系分页与详情 |
| `maintenance/diagnostics.py`、`maintenance/run_bundle.py` | 新字段、关系、版本与消费证据的脱敏导出 |
| `web/src/App.tsx`、`web/src/api/client.ts`、生成类型 | 弱点详情与关系展示，使用生成协议 |

保留现有 middleware 包装顺序 `ResponseRecovery → ContextBuilder → ModelObserver`。
P0/P2 优先扩展现有 WorkingContext；若实施中需拆文件，不增加第二个模型循环。

## 9. API、页面与报告

新增读接口：

```text
GET /api/sessions/{session_id}/weaknesses
GET /api/sessions/{session_id}/weaknesses/{weakness_id}
GET /api/sessions/{session_id}/exploration-relations?weakness_id=...&lead_id=...&cursor=...
```

旧 `/findings` 与验证历史接口保持兼容，返回同一实体；所有访问继续校验项目 owner 和 session。
请求和响应模型放 contracts，通过 export_openapi 和前端生成脚本同步，不手工复制枚举。

首版页面：

- 原发现页升级为弱点列表，显示类型、证据状态、适用条件和关联线索。
- 详情显示“来源弱点 → 当前假设 → 验证结果”关系，按需展开邻居，避免首屏加载全图。
- 计划中同时显示覆盖步骤与推进步骤；同一验证任务服务多条线索时明确列出。
- 暂缓/受阻线索显示缺失条件和最近重评依据；不能使用与已解决相同的视觉状态。
- 报告区分独立缺陷、支持后续探索的条件、未成立的组合假设；不能把弱点数量当作漏洞数量。
- 所有 blocked/deferred 未决项保留在阶段报告中，并说明它们对结论的限制。
- 旧报告版本不重写，旧记录缺少的新字段显示“未记录”，不补造链条。

首版无需复杂全局拓扑编辑器。关系编辑由领域工具完成，UI 采用只读关系详情。

## 10. 迁移、重放和并发

### 10.1 数据库迁移

- 先备份业务库和 checkpoint，核对正在运行的任务，避免在活动操作中更换协议。
- 新字段使用空/未知默认值；旧 Finding 不批量认定为可利用弱点或自动生成关系。
- 旧 blocked/deferred 保留原文和状态，但缺失可推进性判断时进入 needs_review。
- 新关系表初始为空，旧验证外键、报告版本、工件 ID 和操作台账不变。
- 核对现有索引和约束后再生成迁移编号，不预设当前目录最后一个文件就是 Alembic head。

### 10.2 checkpoint 迁移

`CURRENT_STATE_SCHEMA_VERSION` 3→4；迁移链继续兼容 1→2→3→4。

- lead_id 映射到 lead_ids；无依据的步骤 kind 保持兼容默认，不回写虚构历史关系。
- 新呈现/复查状态置空；恢复后从领域表重新组装。
- 保留 messages、消息 ID、原工具调用、next nodes、当前焦点与控制版本。
- 仍通过官方 graph 状态更新接口迁移；保留对未提交并行结果的拒绝猜测行为。
- 读取旧计划和旧工具参数不触发新的真实执行。

### 10.3 幂等与竞争

- 每次领域写入复用原操作登记机制；关系写入与实体变更原子提交。
- 工具重放不重复创建弱点、条件或关系，不重复推进知识版本。
- 并发更新使用 expected_revision；旧响应不得覆盖后来得到的条件和证据。
- 合并与新关系同时发生时，以规范主线索重新核对，避免关系挂在已失效别名上。
- 条件更新与结束复查竞争时，最终放行检查必须发现旧判断已失效。
- 模型请求失败或压缩未发送对象时，不推进该对象的实际呈现记录。

## 11. 验收测试

测试分别证明代码行为、框架接入和真实效果，不能互相替代。

### 11.1 单元测试

新增/扩展建议：`test_weakness_contracts.py`、`test_relation_projection.py`、`test_actionability.py`。

覆盖稳定条件 key、同内容无新增进展、关系去重、依据指纹、别名循环、候选与确认区分、预算整条裁剪。
测试明确的行为边界，不用逐字段镜像实现代替验收。

### 11.2 隔离 PostgreSQL 集成与恢复

建议新增 `test_weakness_relations.py`、`test_chain_planning.py`、`test_completion_inventory.py`、`test_condition_recall.py`。

| 场景 | 必须观察到的结果 |
|---|---|
| 有弱点但没有关联线索 | 下一次决策实际收到该弱点；不能因为关系为空就不呈现 |
| 新弱点提供后续条件 | 模型创建或更新有来源的线索，计划承接，实际操作关联到该方向 |
| 两个候选弱点组合 | 组合保持假设，不能自动得到 confirmed |
| 一个任务服务多条线索 | 一次执行可被复用，但每条线索的条件和结论分别更新 |
| 一种方法失败 | 仅更新相应假设与条件，不自动关闭其他路径 |
| 未申报的未完成线索 | 结束库存仍能找到，不能因漏写 relevant_lead_ids 放行 |
| deferred + 非空理由 | 若仍有可执行下一步，返回 continue |
| blocked + 非空理由但无条件依据 | 需要复查，不作为自动终态 |
| 一条外部受阻，另一条可执行 | 继续可执行路线，不整体 waiting_user |
| 所有有效路线确实需要外部条件 | waiting_user，保留全部未决项 |
| 旧线索合并到主线索 | 复查主对象，关系不丢失，不重复算作两条任务 |
| 超过 200 条线索 | 分页仍能发现后面的未决事项 |
| 复查期间新增弱点或改变身份 | 受影响判断过期，不能按旧快照结束 |
| 来源弱点被反证 | 下游待重评，不自动级联判定所有下游失败 |
| 请求预算排除某对象 | 不标记已发送、已分析或已消费 |
| 暂停/崩溃/工具重放 | 关系不重复、原操作不重发、checkpoint 正确继续 |

更新已有 `test_completion_uses_relevance_explanations_instead_of_score`：保留“不按固定分数阻塞”的原则，移除“未声明相关性即可放行”的断言。

### 11.3 官方 Agent 闭环测试

使用现有 ScriptedModel/合成响应、真实官方 `create_agent` 和隔离业务库，执行：

```text
合成工具结果 → record_weakness → 下一请求含弱点
→ record_lead 建关系 → update_plan
→ 执行 fixture 工具 → 写结果弱点 → 阶段报告
```

断言实际请求 payload、checkpoint、业务关系和操作 execution_context 四者一致。
不能仅 mock 掉 WorkingContext 后断言新增表中有数据。
另测模型始终不消费新弱点时，自主结束返回具体复查反馈，而不是静默结束。

### 11.4 工程与部署门禁

```powershell
uv sync --frozen --all-packages --group dev
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration tests/recovery -q
uv run python scripts/export_openapi.py web/openapi.json
pnpm --dir web generate:api
pnpm --dir web build
```

涉及 Executor 代码时增加相应执行端测试。完成本机依赖/前端、Kali 安装版本/哈希/能力核对和原桌面入口验证。
部署前检查活动任务；真实模型和真实靶场不在自动部署验收中调用。
远端不可达或有活动任务时，如实列出未完成项，不用旧部署证据替代当前状态。
所有新增/修改文件 UTF-8 无 BOM。既有 `.workbuddy` 和实验复核材料保持独立。

## 12. 真实实验与诊断观测

用户执行真实测试，实施方提供编码、部署、合成回归及日志复核。

对第二版日志先固定运行 ID、代码来源、模型、提示版本、关键证据/事件 ID 和目标环境版本。
复盘材料仅保留脱敏元数据和必要摘要，不复制凭据、Cookie、隐藏推理或靶场答案。
禁止从实验案例生成任务专属强提示。

优先验收三条行为链：

1. **Earth 类关系缺失**：原本保存后未继续使用的 Finding，在 V3 中是否实际进入后续判断并改变动作；如果没有，区分未呈现、未理解、未建关系、未执行。
2. **Tomcat8-1 类过早结束**：已存在的未完成线索是否被程序纳入复查，是否因有可执行步骤而继续；不要求靠凭空增加请求数量证明改善。
3. **条件变化场景**：此前受阻或暂缓的线索在获得相关新条件后是否被召回；实际条件没有改善时，是否避免重复相同尝试。

诊断增加 metadata-only 观测：

```text
weakness_created_or_changed(id, revision, changed_condition_keys)
weakness_presented(id, revision, model_call_id, manifest_ref)
weakness_reviewed(id, basis_hash, linked_lead_ids, disposition)
relation_changed(link_id, revision, lead_id, weakness_id, role)
condition_review_due(lead_id, cause_type, source_id, source_revision)
completion_review(result, unreviewed_count, ready_count, blocked_count)
```

复用既有事件和诊断机制，不建立第二份领域事实日志。事件只说明变化和引用，不复制原始证据正文。

报告指标用于定位问题，不规定硬性成功阈值：

- 从弱点保存到实际呈现、用途判断、相关执行的完成决策轮次。
- 有条件描述但长期未被分析的弱点，以及停留在纸面关系而未推进的线索。
- 有依据的连接是否改变了行动，是否出现无证据的组合结论。
- 条件变化后的有效召回、无条件变化的重复召回。
- 结束时遗漏的可推进对象、误等待、无效循环和每决策上下文开销。

不以关系条数、工具调用数或测试全绿代替真实效果判断。弱点“暂未发现用途”可能是正确结论，需结合证据评估。

## 13. 实施批次与完成定义

| 批次 | 交付 | 依赖与退出条件 |
|---|---|---|
| A：协议与迁移基础 | 弱点/线索字段、关系表、领域原子写入、兼容读 | 迁移及幂等测试通过；仍不宣称 P0 完成 |
| B：P0 完整闭环 | 写工具、检索、WorkingContext、计划关联、真实执行关联、关系报告 | A 后实施；官方 Agent 合成闭环通过，证明载体被消费 |
| C：P1 结束判定 | 完整库存、可推进性复查、等待选择、移除状态豁免 | 与 B 并行；独立通过已有线索的结束回归，再接入弱点待分析项 |
| D：P2 条件召回 | 非显式关联旧线索检索、身份/条件触发、去重与失效 | B 稳定后实施；同会话条件变化测试通过 |
| E：集成与交付 | UI、诊断导出、全部迁移恢复、双端部署、桌面入口 | 所有工程门禁与部署结果有当前证据；真实效果另行记录 |

P0 必须以 A+B 的整体交付验收；只有表结构或一组关系写工具不算完成。
P1 的价值可以独立验证，但第三版完整交付要把它与 P0 的待分析弱点接起来。
若分阶段发布，只能明确标注完成的工作包，不把部分完成称为整个 V3 已验收。

## 14. 预计风险与控制点

- **记录变多、行动没改善**：用实际执行关联验收消费闭环；不以弱点/关系数量验收。
- **新标签重演旧门禁漏洞**：no_new_action 必须绑定当前依据并接受矛盾检查；承认语义判断仍依赖模型。
- **条件层变成重型规则系统**：只保存可检索的条件、引用和短判断，不预建全局漏洞本体或完整 DAG。
- **上下文膨胀**：有界摘要、整对象裁剪、分页取证，真实发送 manifest 区分呈现与候选。
- **频繁失效造成空转**：使用实质来源版本，判断元数据不使自己过期；同内容更新无新知识进展。
- **误用旧确认状态**：保留原验证语义，候选条件和组合关系不继承 confirmed。
- **旧任务恢复损坏**：保留旧工具入口和原操作身份，状态迁移不重写消息与待执行节点。

核心完成标准：Agent 能把已有弱点作为后续探索的依据，建立有来源的关系并真正推进；仍有可推进方向时不会通过遗漏或状态改写自行结束。

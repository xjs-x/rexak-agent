# 真实运行问题修复与 Memory 质量优化

日期：2026-10-26。依据：`history-assistance-memory-live-review.md`，Run `eb96c527-9465-4413-89e0-2db49b213f13`。

本轮完成生产代码修复与离线回归。没有重测靶场，没有调用真实 Provider、Executor 或 SSH，没有改这次 Run 的记录或生产配置，没有对现有数据库执行迁移，没有清理数据卷、提交或推送 Git。PostgreSQL 回归使用既有夹具创建的随机临时测试库；仅该空测试库经过测试初始化和清理。

## 1. Memory 选择与内容预算

沿用原 Blackboard、只读 Snapshot 和 `select_memory`，没有新记忆表或 Agent。

- 先确定可信候选优先级，再按实际预算逐条装配；某条过大时继续尝试后面的候选，不先固定截取前三条。
- 保留当前 step、lead、明确引用和来源闭包的相关性。使用操作创建/更新时间、关联工件捕获时间，给最新变化、新的执行证据和未决状态展示机会。旧 Operation 新增工件也能重新进入近期窗口。
- 最新有工件的成功、新近变化和 unknown 保留独立身份；一个没有工件的独立成功不会覆盖另一个 unknown。排序不检查 GET/POST，也不判断行动价值或工具是否应该执行。
- 有这些直接相关候选时，不再为了填满额度展示更旧的回退目录。
- 移除投影内不可配置的 8 KB 上限。`WORKBENCH_HISTORY_ASSISTANCE_INPUT_BYTES` 默认 **16384（16 KiB）**，可配置范围 2–128 KiB；`WORKBENCH_HISTORY_ASSISTANCE_MEMORY_LIMIT` 默认 3。配置是上限，不是投递目标。
- 根据完整模型请求的剩余上下文先缩小参考预算，再用实际 Provider 适配器序列化估算复核；超出时从低优先级条目整条移除，必要时正常回退。没有额外模型调用。

内容采用精简投影：相同条件说明只写一次，共同来源引用集中表示；changed、insufficient 和 comparable_same 保持区分。Operation/Job/Artifact、工件可用性/哈希、身份、Scope/Policy、映射条件与未知状态仍可追溯。Artifact 仍只有引用，不把未读取正文说成已验证证据。

## 2. 增量展示与真实快照比较

按 ToolCall 身份与 Operation ID 识别**当前请求真正保留的结构化工具回执**，不搜索网页正文猜测“模型已经知道”。

- 新结果先给精简补充，避免复制原文和已有 Artifact 描述。
- 已处理、仍完整可见、没有新增证据/状态不确定性/条件变化的匿名确定结果不再复述。身份或目标映射等重要条件缺失时仍保留补充，不能为了去重丢掉适用条件。
- unknown 仍保留其未知状态和本地同步不确定性；状态改变、新工件或条件改变会改变内容指纹，允许重新展示。单纯轮询时间更新不算实质变化。
- 当前请求已经包含同一参考指纹时不重复添加。没有“上次投递过就永不再发”的跨请求缓存：ContextBuilder 裁剪、压缩或恢复后，原回执/参考不再可见，完整历史可以重新出现。
- manifest 新增检索耗时、候选数、选中引用、内容指纹和省略原因；保留最终角色、位置、字节数及消息哈希，不保存敏感正文。

固定夹具：[memory_live_s045.json](../tests/fixtures/memory_live_s045.json)，只读提取本 Run 的 6 个 Operation、来源元数据和 8 次正常请求消息身份；移除正文、利用参数、认证材料和推理，目标换为保留域名 `lab01.invalid`。域名与原地址同为 13 个 ASCII 字符，避免地址长度改变比较口径。SHA256：`237f9d7735a8c61061e102aed2e08faccc4340ac73b15ff766a98ff72a04034b`。

下表比较相同快照、相同请求边界的**参考 bundle UTF-8 字节数**。旧投影重现复盘中的 41,194 字节；新投影使用默认 16 KiB/3 条限制。它不是实际供应商 Token 或真实 off 对照。

| 正常请求 | 原参考字节 | 新参考字节 | 新选择 |
|---|---:|---:|---|
| N1 | 0 | 0 | 无历史 |
| N2 | 5,878 | 2,435 | 新首页 GET |
| N3 | 5,878 | 2,435 | 最新 showcase GET；旧首页已在当前请求中 |
| N4 | 5,878 | 2,335 | unknown POST U1 |
| N5 | 5,878 | 2,335 | unknown POST U1 |
| N6 | 5,878 | 3,947 | unknown POST U2、U1 |
| N7 | 5,902 | 5,659 | 最新成功 POST S、U2、U1 |
| N8 | 5,902 | 3,947 | U2、U1；S 的原回执及证据读取结果仍在请求中，不再复述 |
| 合计 | **41,194** | **23,093** | **减少 18,101 字节，43.94%** |

U1=`cd9b766f-738a-4180-830c-782c81839d7d`；U2=`8cd58e5e-3f5d-4aa8-915c-49a3d66fd612`；S=`64b83762-24f9-4b9e-bfb0-f8d1d109b782`，均为 Operation ID。

旧版只展示 2 条独立 GET；新版在上述边界展示 5 条独立 Operation、11 条次，包含关键未知和成功证据。N7 三条同时进入参考；裁剪掉当前工具消息后，S/U2/U1 完整参考同时返回，bundle **9,575 字节**，未超 16 KiB。N8 的 S 被省略有明确的当前可见依据，不是永久去重。

改善的是选择内容与表示开销，不代表真实模型一定更会利用历史；本轮没有测量真实 Token 节省、延迟或安全测试效果。

## 3. GraphReview 两次无效请求

可确认的事实：当时维护 manifest 已只有 `get_graph/update_graph`，不能认定生产绑定丢失了 Schema；模型仍两次提出 HTTP。原逻辑复用普通测试 System/对话上下文、`tool_choice=None`，并将非法工具列为可立即格式修复的结果，导致同一窗口再次请求。

修复：

1. 维护请求使用专用任务说明和真实的两个图工具 Schema，设置 `tool_choice="required"`。普通安全测试 System Prompt、普通工具集合与权限没有改变。
2. 保留维护格式修正提示，但非法工具一出现就记录结果、关闭窗口并返回普通决策，不在同上下文立即再请求。
3. 去掉非法工具的 `repair_pending`；恢复时若持久回执最后结果为非法工具，也先关闭窗口，防止在“错误已记录、退出前中断”后再花请求。
4. 正常 `get_graph/update_graph`、真实来源校验和有界的图格式/参数修正继续可用。供应商异常仍按原流程记录并抛出，没有吞掉真实错误。

使用真实 `BailianChatOpenAI` 适配器接 `httpx.MockTransport`，确认替身出站中的工具集合与 required 字段；官方 create_agent/Saver 回归验证正常更新、空确认、非法输出退出、暂停取消与恢复。非法输出场景从 2 次维护请求变成 **1 次后退出**。

仍未确认：真实模型为什么忽略当时的 Schema，以及真实服务未来是否严格执行 required。没有以此为由出站，不能宣称以后绝无非法输出。合法图格式修正仍最多两次，不是把所有维护功能关闭。

## 4. unknown Job 与 waiting_tool

根因可由既有证据和源码解释：第一条 POST 为 `RemoteProtocolError/incomplete chunked read`；第二条为 `LocalProtocolError/Illegal header value`。Executor 的 HTTPError 分支都保守写入 unknown，控制端持久状态与之吻合。第二种异常看起来发生在客户端，但不能仅凭错误文字改写成权威“未发送”。

现有恢复逻辑会查询远端 Job 并更新本地状态；若权威结果仍 unknown，继续标 uncertain，不自动重发。Runner 在最终回复保存后仍检查未决 Job/Operation，因此保留 waiting_tool 符合保守设计。

本轮不连接 Executor，**无法确认现在是否存在尚未同步的新权威状态**。没有改这两个 Job、Run、Runner 状态机或执行端源码，也没有给 unknown 人工填成功/失败。

补充展示：API 公开一个布尔值 `model_response_saved`，只在最终报告指针对应当前最新正常模型调用时成立，不公开整份模型配置。新决策出现后旧报告指针不会冒充当前已结束。前端在 waiting_tool 且该标记成立时显示：

> 模型已结束并保存最终回复，但仍有未决操作；执行是否完成尚未确认，不会自动重发。

提供“查看未决操作”入口，进入现有后台任务页；不启动模型、不发起新工具执行，不把按钮做成自动重试。

## 5. 改动位置

- Memory：`domain/decision_memory.py`、`agent/history_assistance.py`、`agent/middleware/history_assistance.py`、`config/settings.py`、`.env.example`。
- 图维护：`agent/middleware/response_recovery.py`、`agent/middleware/context_builder.py`、`domain/graph_sync.py`。
- 展示：`contracts/models.py`、`api/routes.py`、`web/src/App.tsx`；由实际 OpenAPI 重新生成前端类型，未手写第二套协议。
- 回归：新增 `tests/unit/test_memory_live_quality.py`、`test_graph_review_wire_quality.py` 和一个脱敏 JSON 夹具；扩展 `test_session_graph_review.py`、`test_natural_completion_safety.py`。旧 Decision Memory 集成夹具显式固定 legacy/off，避免用户 `.env=memory` 改变其测试含义，未删除原断言。

没有改 `factory.py` 的官方 create_agent 主循环装配、Runner 或 Executor；没有修改旧 Oracle、冻结输入、历史调用账本或复盘报告。

## 6. 验证

定向单测 **47 passed**：真实快照选择、旧历史增多、HTTP 方法不影响排名、新工件/状态变化、字节节省、当前请求去重、裁剪后召回、缺失身份条件、真实适配器 MockTransport、上下文剩余额度、off 与控制信号等。

相关集成 **68 passed**：

```text
tests/integration/test_history_assistance_mvp.py
tests/integration/test_decision_memory.py
tests/integration/test_session_graph_review.py
tests/integration/test_natural_completion_safety.py
tests/integration/test_request_budget.py
```

合计 **115 项不同用例通过**，没有运行旧科研正式评价。覆盖 off/memory/passive、正常模型调用数不增加、真实 PostgreSQL/Saver、工具配对与原结果不变、提示去重与恢复、未知状态不自动重放、结束状态保留和公开展示。集成测试仍有 12 条来自既有 SimpleNamespace Toolbox 测试替身的 Pydantic 序列化警告，没有隐藏或关闭这些警告。

- 本轮修改的 Python 源码及测试 Ruff：通过。
- 全项目 Pyright：0 errors / 0 warnings。
- UTF-8 检查：通过；新增夹具和报告另检查 UTF-8 无 BOM、无替换字符/指定乱码。
- 前端 TypeScript 与 Vite 生产构建：通过。
- 全仓 Ruff：仍有 **7 项旧研究归档问题**，位于 `experiments/dual_graph_replay/results`、`evidence_action_control_reliability_v1`、`history_action_autonomous_shadow_v1/baseline` 和 `history_action_reflection_preparation_v1/test-source-before-typing`；没有修改冻结材料来消除这些问题，不能称全仓 lint 全绿。

本轮开始记录了 Git 工作区及相关源码 SHA256 基线。复盘报告 SHA256 仍为 `de9247954c37aa174b1c0311b6cb148b4c14d6a9d7a1dcb740ed4b334a13c09c`，`uv.lock`、原 `runner.py`、`factory.py` 哈希保持不变。原 loaded_code_version 对全进程溯源的限制继续保留，没有扩大为本轮重构。

## 7. 使用与剩余限制

默认仍为 off，已有 Run 的模式固定方式和强制关闭入口不变。后端可配置：

```dotenv
WORKBENCH_HISTORY_ASSISTANCE_MODE=memory
WORKBENCH_HISTORY_ASSISTANCE_INPUT_BYTES=16384
WORKBENCH_HISTORY_ASSISTANCE_MEMORY_LIMIT=3
```

16 KiB 只是参考上限，正常完整请求仍受模型上下文预算保护；紧张时少投或不投。切 passive 不会开放固定测试配方权限，也没有改变普通 HTTP 的可信检测覆盖。

本轮交付的是代码、前端构建与离线验证，**没有重启正在运行的生产 API/Runner，也没有确认它们已加载新版**。按本轮禁止自动连接 Executor 的要求，没有运行原桌面启动验收或同步 Kali；原启动器会检查执行端并可能推进任务，不能为了验收突破此边界。后续由用户保全当前任务状态后重启原入口加载，数据库无需迁移。

已证实并修复的是“旧记录占位、冗长重复投递、非法维护立即重复请求、未决结束展示不清”。真实模型是否更好利用新参考、真实 Provider 是否遵守 required、远端两个未知 Job 的最新权威状态，仍待未来明确授权，不能由离线通过替代。

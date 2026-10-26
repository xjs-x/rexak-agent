# Memory 最后一轮工程修复与收尾

日期：2026-10-26。依据第二次真实复盘，Run `02f7f4b7-e64f-450c-ac48-f0729c6c1843`。

本轮完成三个已确认缺口：可见回执重复投递、检索状态歧义、运行代码指纹不稳定。没有修改排序算法、16 KiB 默认预算、Shadow 覆盖、GraphReview 或 Executor，没有运行新的真实测试。历史 Run、配置、旧报告和冻结材料未被改写。

## 1. 回执去重修复

根因是用“ToolMessage 后是否有 AIMessage”判断 processed，导致刚完成的原结果虽然已在当前请求里，仍被 Memory 复述。这个顺序关系也不能证明模型此前读取或理解过结果。

现在直接比较当前请求中的可信结构化回执：

- 先匹配原 ToolCall ID 与 Operation ID，不解析网页或工件正文来猜测已知事实。
- 原回执完整、状态及结果一致、没有新工件或必要条件补充时，不添加参考；不再依赖后续 AIMessage。
- Job 状态/同步不确定性、资源状态/执行端、Artifact、身份、映射、环境或条件发生实质变化时，仍投递增量资料；unknown 保持未知，不被映射为失败或成功。
- 原回执被裁剪、归档替换或标记不完整时，可重新召回元数据和 Artifact 引用。没有永久“已发送”缓存。
- 同一参考仍在当前输入中时，忽略单纯 last_synced_at/last_checked_at 轮询时间变化，避免将其当作新事实。正文仍未由 Memory 检索读取。

排序和候选来源没有改变，Scope/Session/Run 授权边界不变。

## 2. 同一真实快照的前后结果

新增单个脱敏固定夹具 `tests/fixtures/memory_second_live_s045.json`：11个 Operation、8次正常请求的可见消息身份及结构化状态，删除请求参数、正文、认证和推理，目标替换为保留域名。SHA256：`bb4266e714d64821f5673f3142d45427ee04864275e35c87c68f8ec7cbba2179`。同时复用原六操作快照回归，没有开发实验驱动器。

| 正常请求 | 修复前参考字节 | 修复后参考字节 | 准确原因 |
|---|---:|---:|---|
| N1 | 0 | 0 | no_relevant_history |
| N2 | 4205 | 0 | already_visible，2个候选 |
| N3 | 2493 | 0 | already_visible，2个候选 |
| N4 | 2493 | 0 | already_visible，1个候选 |
| N5 | 2493 | 0 | already_visible，1个候选 |
| N6 | 0 | 0 | already_visible，1个候选；不再误称没有历史 |
| N7 | 2493 | 0 | already_visible，2个候选 |
| N8 | 2493 | 0 | already_visible，2个候选 |
| 合计 | **16670** | **0** | 7条重复参考不再发送 |

这是相同历史快照的**额外参考字节**比较，不是新的真实模型运行或计费 Token 实验。原 ToolMessage 中的结果、身份回显、Job 状态和 Artifact 引用仍在输入中，不是把真实证据删掉了。

裁剪掉原回执后，最后请求可重新召回2个原有候选，Memory子树6703字节，仍在16 KiB上限内。新状态、新 Artifact、身份/条件变化、部分回执和 unknown 的单测均保留了必要信息。没有宣称这次真实 Run 出现过 unknown；unknown 的验证来自既有脱敏六操作快照和离线状态变化用例。

## 3. manifest 状态与字节数

替换歧义状态 no_relevant_bounded_history，明确记录：

- `no_relevant_history`：没有合格相关候选。
- `already_visible`：有相关历史，但当前输入已完整包含所需事实。
- `omitted_by_budget`：相关资料受条数、参考字节或完整上下文预算省略。
- `retrieval_timeout` / `retrieval_error`：超时或检索异常。异常时候选数量为未知，不伪造为0。
- `selected`：确有资料进入参考；额外投影错误独立标 `projection_error`。

保留候选数量、选中引用及逐项省略原因。未投递时 bytes=0；部分预算裁剪后，selected_refs 和 projection_bytes 按最终视图修正。新增 memory_bytes/reflection_bytes，区分子树与整个参考，避免把 passive 提示字节误算为 Memory。另记录实际参考上限和条数上限，原16 KiB默认值未变。

## 4. 已加载代码指纹

已离线复现旧算法缺陷：不改变函数代码，仅保留其 co_consts/co_names/co_code 等对象引用，marshal.dumps(code) 的结果就变化；旧指纹因此可能受引用状态影响。加上它未覆盖主要 Memory 文件，启动日志与 Run 指纹不能作为可靠的一致性判断。

改为 `canonical_code_v2`：稳定序列化内存代码对象的字节码、常量、名称、参数、默认值及异常表等字段，消除 marshal 引用标志、地址和文件路径因素；不读取磁盘源码。新增 `loaded-runtime-v2:` 标识。覆盖原入口、两个 Memory 文件中的函数、HistoryAssistanceMiddleware 方法及请求实际使用的导入绑定。

现有位置各自记录明确来源：

| 位置 | 含义 |
|---|---|
| API/Runner 初始化 | process_start，含阶段、进程ID与捕获时间 |
| 新 Run 创建 | code_version仍为unbound；runtime_at_creation明确尚未由Runner绑定 |
| Agent 装配 | run_assembly，取当时内存中的实际实现 |
| 每次请求 manifest | 同时保存上述进程/装配版本，以及独立标注的disk_source_at_activation |
| 历史激活记录 | 保留原条目，仅在实际新激活时追加；不重算或改写旧指纹 |

跨引用持有、不同 PYTHONHASHSEED 和独立进程的相同实现指纹一致；替换实际 Memory 绑定会改变指纹；禁止 Path.read_bytes 后仍能计算。范围仍显式为“入口与历史辅助实现，不是整个进程”，没有将磁盘树 SHA 冒充已加载代码，也没有建立新的溯源数据库。

## 5. 验证与兼容性

相关 **117 个不同 pytest 用例通过**：完整组116项通过后，补充了“投影异常不能冒充检索失败”的回归；最终复跑67项单测和17项关键集成也通过。覆盖最新及旧六操作快照、完整回执去重、状态/工件/条件变化、裁剪召回、异常标签、指纹稳定性、旧激活条目保留，以及生产 API/Runner/create_agent/Saver 的 off/memory/passive 回归。

Runner测试仍为4次确定性模型调用、3个工具操作；新增请求视图裁剪场景能召回 Memory，原 ToolCall、ToolMessage、业务结果和 checkpoint 未混入参考。passive 提示及去重、可信配方边界、关闭和恢复行为继续通过，没有开放额外工具权限。

- 相关源码和测试 Ruff通过；Pyright为0 errors/0 warnings。
- UTF-8无BOM检查通过；前端TypeScript检查通过。本轮无前端代码或接口枚举改动。
- 全仓Ruff仍有7项既有研究归档问题，未修改冻结材料消除它们。
- `domain/decision_memory.py`、`config/settings.py`、GraphReview相关实现及3份既有报告的SHA256与本轮基线一致；依赖锁文件不变。
- PostgreSQL回归仅初始化并清理随机临时测试库，未迁移生产库；未调用真实模型、Executor、SSH或靶场，未提交或推送Git。

改动集中在 Memory 投影/middleware、factory稳定指纹、既有CLI/Runner/API的版本元数据接缝、请求manifest，以及相关测试。Runner状态机、GraphReview行为和执行端没有重构。

## 6. 交付边界

代码与离线验收已完成；本轮没有重启生产API/Runner，不能声称运行进程已加载最终修复。原桌面入口会检查Executor，因此按本轮禁令没有启动它。历史 Run 仍保留原指纹；无需数据库迁移，后续正常重启加载新版即可记录v2来源。

**Memory按当前功能范围视为已交付。** 除明确功能性Bug外，不再继续围绕内部排序、压缩或16 KiB预算优化。后续独立工作是跨阶段证据复用、run_command输出持久化，以及终报引用、停止时机等实际任务质量。GraphReview只读空转、命令缺少Artifact等问题仍保留，未借本轮扩大修复范围。

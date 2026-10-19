# 0009：所有 checkpoint 写入口统一验证所有权与租约

日期：2026-10-19。状态：已实现，专项与独立反例通过。

普通 Runner 和迁移必须使用同一写入门禁。只检查 generation / control / epoch 不足以撤销尚未接管但已经 release 的 token。

`FencedCheckpointSaver._lock_head` 对 aput 与 aput_writes 统一要求 run 有 owner、有未过期的 lease；缺失或过期都在调用原 Saver 前抛 StaleWriterError。Runner 将自己的 owner 传入 runtime，再绑定到 FencedSaver；迁移继续绑定其独立 owner。两条入口保留原有 generation、control、epoch 和基线 CAS 检查。

隔离反例覆盖 released / missing_lease / expired × 两种方法，修复前 3 failed / 3 passed，修复后全部通过，且拒绝时原 Saver 未被调用。旧图迁移探针的 checkpoint seed 也必须先 claim、写入后 release，不能将无主写入视为合法夹具。

该变更不启动 Runner、不重放任务，不修改业务 / 框架 schema，也不触发模型调用。

## 完整方案中的两处语义澄清

confirmed 的正确约束是 `status <> 'confirmed' OR verification_id IS NOT NULL`，含义是 confirmed 必须带验证记录。将前半写成等号会使 confirmed 绕过要求，不能照抄。

旧计划迁移保留既有 session / step / v1 的确定性 mutation；同一个旧步骤内容变化返回结构化 migration_conflict，不自动改内容生成新 mutation 来绕过冲突。这与完整方案要求的“源变更保留现场、等待对账”一致。

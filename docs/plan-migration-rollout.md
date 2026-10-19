# 旧计划迁移的单会话与批次门禁

当前实现沿用已有 `migrate-legacy-plan` CLI、checkpoint fence 和 append-only 合同版本，不新增调度器或第二份计划状态。

## 单会话 canary

先通过 profile、P12 迁移审计、主探针、全量测试和静态检查；提交产品修改，确认工作区干净、数据库版本匹配。真实 apply 必须来自用户授权，并由执行者逐会话确认门禁。

为当前代码与数据库生成新备份，包含数据库和工件，再运行 `verify-backup`。备份完整性校验不等于已经做过恢复演练；旧数据库版本的备份不能冒充当前恢复点。

重新 dry-run，核对 session、run、paused / queued 状态、session 内所有 run 的租约、四类业务 blockers、未闭合调用、control_version 与具体 base checkpoint。任何变化都停止，不能沿用上次的 eligible 结果。running 即使没有有效租约，也必须先对账和安全暂停。

单次 apply 后立即检查：

- 每个旧步骤对应一份被 checkpoint 精确选中的合同，refs 覆盖率 100%，无重复或孤立引用。
- 旧 done 仅映射为 legacy_done；不能使 finish gate 直接完成。
- accepted head 非空并推进；API `/plan` 返回 `plan_contracts`、可读 frontier 与正确 active IDs。旧步骤仅作只读投影。
- 迁移前的旧 Saver 在 `aput`、`aput_writes` 两条路径均被拒绝。
- ownership 释放、状态与 control 保留；其他会话、业务记录、执行台账和模型调用没有额外变化。

只对账和整合已存在的结果；不恢复暂停会话，不重放 uncertain 工具调用，不启动能认领历史任务的 Runner。未通过则停止，不删除合同版本假装回滚。

## 批次策略

单会话 canary 通过后，才可以另行批准 1 / 5 / 20 的批次。**本文不是批量 apply 授权，也不自动执行任何批次。** 每条 session 都需独立 dry-run、固定基线、备份校验和执行者批准；不能把一个会话的通过扩展为全批授权。

批次清单应记录 session / run、备份路径与 hash、base checkpoint、control / generation / epoch、旧步骤与 done 数量、blockers、批准记录以及 apply / API / head / finish gate 结果。先顺序执行，每条验完再进入下一条；任一拒绝、基线变化、未决结果或验收失败都停止扩批。

1 / 5 / 20 表示批次容量上限；合格候选不足时只迁移实际合格数量，不为凑数迁移受阻、删除或待人工核对的会话。frontier 默认只返回近端 5 项，验收应与确定性 frontier 算法逐项比较，不能要求 frontier 数量等于全部合同数。

批量前置清单可以只读生成，但不得自动改状态、清理历史版本或跳过单会话门禁。后续关闭 steps_json、frontier 自动调度、输出阈值和报告模板不属于此迁移链路。

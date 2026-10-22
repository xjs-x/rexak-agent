# 可选工作记录与并行

可以直接调用授权工具，无需预建计划或登记每次观察。

需要计划时，update_plan 使用 expected_plan_revision 和 steps 数组；每项含 step_id，
可选 description、status、rationale、lead_id。只提交要改的步骤，其余保留。
focus 是可选调查方向，不需要步骤或线索。完成只表示工作记录，不代表漏洞验证。

普通反思在正常模型—工具循环中完成，不存在 Assessment 表单、CompletionSpec 或完成证明。
关键假设可选择 record_lead，关键现象可选择 record_observation，候选问题用 propose_finding。

并行工具继承派发时的原状态，不能假设同批 update_plan 已生效；依赖结果的动作放下一轮。
同批最多一次 update_plan。已有 operation_id 与工件原归属不会因计划变化而重写。

直接输出最终回复结束，无需结束工具或证明。结果说明未验证项和局限；未决执行与 unknown
仍由 Runner 核对，不得为结束而重放有副作用操作。

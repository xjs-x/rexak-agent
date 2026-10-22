# 0015：模型自然结束与运行安全收尾

日期：2026-10-22。按第二轮用户要求，取代 0014 的 FinishGate 与结束工具约定。

官方 `create_agent` 是唯一模型—工具循环。模型不再申请结束；正常 END、没有后续节点且最后一条 AIMessage 没有工具调用时，Runner 保存真实最终回复。新运行的结束原因为 `model_finished`，不判断目标是否达成，也不提升候选 Finding。

新图不装配 FinishGate，不注册 `propose_completion`，不新写 CompletionProposal 或结束反馈。旧 FinishGate 节点只作为无副作用的 checkpoint 恢复桥；旧提议通道仅供历史读取，不能触发完成。状态版本 8 保留原消息、操作身份和待执行节点，无法保持边界或含未提交并行结果时明确拒绝迁移。

Runner 只核对当前 Run 的租约、代次、控制版本、暂停/取消/追加指令、已派发工作和 checkpoint 边界。分段 yield 不等于完成。未决 Job/等待集合进入 waiting_tool；没有可核对 Job 的未决 Operation 或有限资源进入 recovering。不向模型发回业务审查意见，不读取 Plan/Lead/Coverage/Finding 或结束证据判断是否允许完成。

HTTP 接收错误不能证明目标未执行。新 Executor 将传输错误记为 unknown；控制端读取旧 failed 传输回执时保留其错误码并投影为 unknown/uncertain。恢复沿原身份查询，不自动重做未知副作用；模型的新 ToolCall 仍可产生新 Operation，不实施命令文本语义去重。

原始操作、工件、哈希、归属与领域记录自身的真实性检查保留。日志导出只关联可靠确定的同会话原 Operation，不能确定时保留未知来源。最终文本与自然语言工件引用不是 Verification。

验收与限制见 [第二轮实现报告](../../reports/finishgate-removal/implementation-review.md)。不引入新 Planner、完成审批、Verification、信息图或记忆架构，不自动发起真实模型与靶场实验。

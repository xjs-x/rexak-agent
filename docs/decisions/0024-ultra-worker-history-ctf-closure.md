# 0024：Ultra Worker 历史续用与目标收尾

日期：2026-11-04。继承 0019–0023 与 Stage 3 修复；不升级 Pi/A2A，不新增 Agent 循环。

- delegate_task 可选 resume_from_delegation_id；控制端只允许本 Session、completed、未取消且有 context_id 的来源。Node 另核对来源请求完成、A2A completed、Session 一致和真实历史。旧 context_id 没有历史不代表可恢复。
- 新 Delegation/Task/Worker/context/Operation 身份全部独立。历史只是低信任消息，不复制 Scope、凭据、系统提示、工具定义、执行闭包、取消信号、运行句柄、未决操作或 steering 队列。新授权继续由当前 Session/Run 的原守卫生成。
- Pi 1.5.0 使用 Agent.initialState.messages 恢复完整已完成轮次，再 agent.prompt 新任务；不是重开终态 Task，也不是 continue 原执行。continue 只适用于末尾 user/toolResult 的原生续跑，本阶段不恢复中断任务。
- ultra_worker.histories 保存版本化消息投影，和 A2A completed 原子提交。移除 system、thinking、opaque 签名及 details；保留 user、assistant text/toolCall 与配对 toolResult。凭据形态文本脱敏，工具响应正文沿用已有有界输出，完整日志仍在 Artifact。模型可能把敏感内容改写成无法识别形式，脱敏不是任意秘密识别器。
- 规范化 JSON SHA256 适配 JSONB 键序；读取时检查版本、大小、轮次完成、ToolCall/Result 配对和允许字段。最多 256 消息、96 KiB；超限保留任务真实完成，标记历史不可用，后续恢复明确失败。没有静默截断工具配对或虚报无损恢复。
- Pi 原生 transformContext 在每次模型请求前执行保守容量检查，预留输出和系统/工具空间；超限明确建议新委派配合证据引用。没有自动摘要 Agent、跨任务记忆框架或任务总时长/费用限制。
- Stage 4 的运行中补充仍走原 Agent.steer，不从快照注入；重启只保留已完成历史，运行中任务仍 interrupted/unknown，不恢复、重放未知操作。
- CTF/指定目标由 Main 自主判断，优先使用实际成功/平台证据，否则轻量判断并说明不确定性。不设 Flag 格式、固定失败轮数或独立验证 Agent。Main 利用中途候选推进，达成后取消无用 Worker，通过实际 Operation/Artifact/协作事件输出技术复盘。候选不创建 confirmed。
- 取消回执与资源停止分开；cancel_requested/unknown 必须披露。原 Runner 在等待执行对账前保存最终回复，不能将结果输出与远端已停止混为一谈。真实未知资源仍沿原机制保留等待/恢复状态。

验收与部署以 [Stage 5 报告](../../reports/ultra-goal-stage5/implementation-report.md) 为准。

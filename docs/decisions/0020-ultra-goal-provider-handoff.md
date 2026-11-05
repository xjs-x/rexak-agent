# 0020：Ultra Goal 第一阶段模型与交接

日期：2026-11-04（北京时间）。在 0019 及审查补修基础上，只扩展真实模型与任务交接，不接入真实 Executor。

- Standard 沿用原 Python create_agent、Runner 和授权路径。Worker 仍使用 Pi 1.5.0 Agent Loop；真实模型通过 Pi 原生 `api/openai-completions.stream`，不新增工具调用循环，不升级锁定 SDK。
- Worker 模型配置是独立的后端快照。显式配置时复制既有供应商档案、端点与密钥；后续 Main 配置改变不会隐式改变 Worker。Windows 用当前用户 DPAPI；密钥只在可信启动环境传给 Node，不进入 A2A、模型对象、业务表或结果工件。配置无效必须失败，不降级 Mock。显式 `--mock` 保留测试入口。
- 本机选择百炼 `deepseek-v4.1-flash`，温度 1、输出上限 393216、上下文 1000000、单次请求超时 300 秒；SDK 和 Pi 请求重试均为 0。没有任务总时长或费用硬上限。Node 使用每次请求的整体 AbortSignal 超时，不能宣称与 Python HTTPX 的连接/读写分段超时完全相同。
- `DelegationInput` 增加 background 与可选 completion_requirements。Python 校验同 Session、available 的 Artifact 引用，补充 SHA256、大小、媒体类型、采集时间、来源 Operation ID；不传文件路径、目标 URL、凭据、私有消息历史或自动检索结果。不解引用正文，必要背景由 Main 明确交接。
- 版本化 `WorkerTaskPackage` 和来源类型以 Python contracts 为真源，Node 校验对应字段。任务包先落 `handoff_snapshot` 再发 A2A；输入摘要与来源快照分开，重放不重新构造已提交来源。旧 objective/context_refs 摘要保持兼容。
- context_id 是 delegation_id 派生的 UUIDv5，同时作为新任务 A2A Context ID 和 Pi sessionId。模型不能指定身份；新任务仍产生新 context_id，不实现历史恢复或跨任务复用。旧记录保持 null，旧协议仍可工作。
- Provider 调用前写 `ultra_worker.model_calls` 意图，结束后写耗时、用量及安全错误分类；不保存请求正文、思考文本或供应商原始错误。无 usage 或中断保持 unknown/null，不假设免费。Pi 的零价格占位不作为费用证据，Worker 用量暂不并入 Main 的 model_calls 计费。
- 模型结果仍为低信任描述；工具观察仍标 mock_tool，evidence_refs 为空。结果与 Task 的 context_id 必须对应本地快照；坏结果拒绝写工件，但保留已核实的执行终态。现有取消、失锁 fence、unknown 不重发和不可恢复 Pi 循环语义不变。

实际测试、部署证据和下一阶段接口见 [实施报告](../../reports/ultra-goal-stage1/implementation-report.md)。一项真实模型任务完成三次调用和两次 Mock 工具执行，不代表 CTF 目标效果。

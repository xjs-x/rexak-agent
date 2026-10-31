# 0018：v1.0 工程收尾与废弃路径移除

2026-10-30。依据用户明确要求执行工程收尾，不新增 Agent 机制。

正式历史辅助仅保留 memory/off，新环境默认 memory；旧 Run 缺模式保持 off，旧 passive 快照映射 memory。
移除 passive 检测器、Shadow 配方/授权/反馈存储和 Progress 运行路径；LegacyProtocolState 的旧字段只为历史读取保留。
Decision Memory、WorkingContext、ContextBuilder、同会话来源检查和执行身份/幂等/租约/恢复不变。

图查询按 Session/owner 鉴权；前端增加手动刷新，真实会话失败不回落样例。
增量迁移 f19a30b742c1 恢复正式资产写入所需的 method+identity 唯一约束；不合并或删除历史行，
历史重复键会使迁移事务显式失败，不能自动清理。日常环境有未解决队列/活动任务时不强制上线。

工程验收、隔离测试、实际浏览器验证和正式部署分别报告，见 reports/v1-finalization.md。

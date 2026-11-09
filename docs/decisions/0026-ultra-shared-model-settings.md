# 0026：Main 与 Pi Worker 共用前端模型设置

日期：2026-11-08。根据用户明确要求，替代 decision 0020 中“Worker 独立模型快照”的规定，其余任务交接、权限和执行规则保持。

- 模型设置和本机模型密钥库是唯一可写配置来源。桌面入口不再读取或传递 `ultra-worker-model.json`，历史文件保留，不删除或覆盖。
- 前端保存的默认模型适用于新阶段；Worker 启动时从可信回环控制端通道取得父 Run 的模型绑定、参数与共用密钥。主任务有显式模型选择或继续历史任务时，Worker 跟随该任务绑定，不擅自切成新的全局默认模型。
- 在途 Main/Worker 保留本次请求/任务绑定；更新 Key 后，新阶段、暂停恢复及之后启动的 Worker 读取新密钥。不打断正在执行的模型请求或工具，不重启 Worker 来刷新配置。
- 通道要求回环来源和 Worker 服务令牌；有 delegation_id 时核对所属 Session、Ultra Run、控制版本、租约和撤销状态。响应 `Cache-Control: no-store`；密钥只进入可信 Worker 运行内存，不进入 A2A 任务包、模型上下文、工件、日志或 Kali。
- 无有效配置时明确失败，不使用历史独立密钥或降级 Mock。显式 `--mock` 仍供隔离测试使用。兼容 `--configure-model` 命令改为选择共用默认模型，不再复制密钥。
- Worker health 报告 `model_source=controller_shared` 与共用默认模型，仅说明配置读取链可用，不证明供应商认证或真实模型调用成功。

本次验证及部署证据见 `reports/shared-model-settings-20261108.md`。

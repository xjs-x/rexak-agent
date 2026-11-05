# 0022：Ultra Goal 第三阶段 Pi 基础工具接入 Executor

日期：2026-11-04。继承 0019–0021；本决策仅扩展新委派的执行能力。

- Main 继续官方 create_agent，Worker 继续 pi-agent-core 1.5.0 原生循环。新任务包增加可选 execution_access=pi_tools_v1，权限固定到 handoff_snapshot 和 authorization_snapshot；旧快照不回填，Standard 不依赖 Worker。
- 模型只看到 read、bash、edit、write、grep、find、list_evidence、read_evidence 八个工具。无 mock_observe 同义入口；历史无执行授权任务仍使用原 Mock 工具集。
- 研究 npm 官方 pi-coding-agent 1.5.0 工具源码后，采用窄范围 MIT 源码复用，没有引入完整 pi-coding-agent 依赖或会话框架。保留官方 Schema、精确编辑算法、差异与文本截断/主要格式化代码；生成脚本和源/派生哈希见 worker/vendor/pi-tools/provenance.json。
- read/write/edit/find/bash 工厂通过 Operations 连接控制端。去除默认本地文件/进程、UI、图像解码及临时输出文件实现，路径使用 POSIX。grep 原工厂不支持替换实际 rg 进程，因此仅复用其 Schema/说明/截断，以薄 execute 适配远程 rg。bash 输出累积保存在有界内存，完整日志由 Executor 负责。
- 固定回环控制端通道沿用 Stage 2 服务身份，工具通道使用独立 tools:v1 HMAC 域，绑定 delegation/task/worker/context。每次核对 A2A 持久身份、父 Session/Run 所有权、取消/租约/generation/control/工具授权；不向模型暴露凭据、回调地址或数据库。
- 每个 Pi ToolCall 先登记聚合意图，再按稳定 step 派生叶操作。身份由 delegation_id + Pi ToolCall 派生；重放相同参数只能查询已有叶操作，参数变化拒绝。响应丢失、404、协议/输出不确定不触发重新派发；Node 中止当前循环，A2A 保留 interrupted/UNSPECIFIED，不能声称失败或已取消。
- 叶操作继续 OperationRegistry → run_execution_kernel → 相同父 Run fence → execution/v1 → CommandRuntime。Worker 身份使用 envelope.delegation_id + revision=1，未创建第二套可写任务图或改写既有 Duty delegation。撤销列表扩展包含已取消/终结 Worker；控制同步与执行接收共用 admission lock。
- Main 与 Worker 共用父 Run owner/generation/control，分别拥有 Operation/Resource/Artifact 身份。每个 Worker 的 cwd 是 Session 工作区内 .rexak/workers/<delegation_id>；文件工具拒绝越界和非 POSIX 路径，edit 使用读入 SHA 的乐观并发检查。Kali worker_files 模块作为普通命令 Job 执行，文件写入不伪装为纯读取。
- 这是沿用已有 trusted 命令授权的目录隔离，不是 Linux 用户/容器沙箱。bash 能调用当前 Executor 用户获准使用的程序，不能承诺阻止故意越过 cwd 的任意 Shell；也不提供浏览器/Cookie 资源的自动独占分配。需要更强对抗隔离时应单独设计执行身份，不能放宽 fence。
- Pi 停止后核对所有叶资源；取消先同步负权限再取消本 Worker 的资源，确认停止后才允许取消终态。未知叶资源保留未决；持久 pi_loop_stopped 证明只用于本进程已停止的循环，服务重启的旧 interrupted 任务没有该证明，不支持无损恢复或自动重发。
- 输出复用 Executor 原始日志哈希与 ArtifactStore，聚合结果工件化，checkpoint 不保存完整 Worker 历史。工具结果与模型最终判断分开，Stage 2 的证据读取收据和校验保留，不创建 confirmed Finding。
- 不实现实时发现推送、增量消息、上下文续用、递归委派、任务 DAG 或 Flag 自动收尾。

实际验收结果及限制见 [第三阶段实施报告](../../reports/ultra-goal-stage3/implementation-report.md)。

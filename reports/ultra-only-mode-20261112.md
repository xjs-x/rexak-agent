# 新任务锁定 Ultra 模式：实施与验收

日期：2026-11-12。范围仅为新任务模式选择、历史任务执行拦截和入口验收；未调用付费模型或真实比赛目标，未提交 Git。

## 原默认值与入口

- 模式协议使用 `standard`、`ultra`。Web `useWorkbench`/Composer 与模式下拉框原默认 Standard；两条 Session 创建请求均传入前端状态。
- `SessionCreate`、`SessionRepository.create`、ORM/数据库 `sessions.execution_mode` 原默认 Standard；Run 创建由 Session 模式写入 `config_snapshot`。历史读取 Schema 的 Standard 兜底保留，以免伪造旧数据模式。
- Runner 原从 Run 快照读取模式，缺失时回退 Standard；生产候选扫描、定向领取、启动恢复和后台唤醒原无统一 Ultra 过滤。Ultra Worker/A2A 不可用时原本会明确失败，本次保留该行为。
- TSecBench CLI/配置原默认 Standard；桌面评测 GUI 可选 Standard，旧私有配置也可恢复为 Standard。CTFy MCP 工具由 Main 工具清单配置，未独立选择运行模式。

## 改动

- `SessionCreate` 默认 Ultra，显式 Standard 返回 422，非法枚举仍返回 422。仓储与新 Run 创建也拒绝 Standard；两条 Web 创建请求显式传 Ultra，前端只显示 Ultra 标签。
- 数据库迁移 `c8a7d65f021b` 只将新 Session 列默认值设为 `ultra`，没有新增列或表，也没有更新历史行或证据。实际数据库已迁移到该版本。
- 历史 Standard Session 可查看，但消息续聊、新 Run、显式 resume 被拒绝。Runner 候选扫描与定向驱动在领取前跳过历史 Standard；启动恢复、等待任务唤醒和执行事件活动扫描跳过历史 Standard，不改变原状态。
- TSecBench CLI 默认 Ultra 并拒绝显式 Standard；GUI 只显示 Ultra，旧私有配置和凭据文件保留。原桌面 `start-workbench.cmd` 现在默认连接现有 Kali Docker；Linux 原生 Main/Worker 路径不依赖浏览器状态，仍需部署其 Worker 服务。

## 验证

- 最终相关后端回归（Main Mock、A2A/Pi、Runner、历史导出、CTFy 工具、启动器）96 项通过；Worker 工具代理、A2A 和协作专项 12 项通过。此前含 TSecBench 的定向运行 72 项通过、1 项因缺少可选 SDK 跳过，各组数量可能重叠。Worker 不可用专项另 1 项通过。前端 82 项通过，前端与 Worker 构建通过。Ruff 定向、编码检查、`git diff --check`、`uv sync --frozen --all-packages --group dev` 通过。
- 首轮完整 `unit`/`integration` 回归：1279 passed、11 failed、6 skipped。失败中 10 项涉及旧模式假设或固定迁移版本，已修改并分别定向复测通过；另 1 项为导出 CLI 30 秒超时，独立复测通过。修改后未重复耗时约 9 分钟的全量回归。
- Pyright 全仓 9 项既有测试类型错误；本次修改文件的 Ruff 通过。没有真实模型、赛事、Linux VPS 原生进程或正式无人值守运行验收。
- 真实本机数据库迁移前后：Session 169、Run 174、Artifact 9726、Operation 3797；非终态仍为 Standard paused 5、Ultra paused 8、Ultra recovering 3。数据库列默认值实测为 `ultra`。
- Docker PostgreSQL 与 Kali 容器 healthy；原 `start-workbench.cmd` 无需额外 Docker 参数，`--api-only --no-recover` 短时启动的 API、前端和 Worker 就绪，HTTP `/health`、`/console/`、`/openapi.json` 均 200。Kali 容器中 27 个 Executor Python 文件哈希与本机一致，未更新或重启现有进程。只读探针使用 SSH 偶发超时，因此哈希改以容器内只读命令核对；原入口 SSH/Executor 能力检查通过。探针证据位于 Git 忽略的 `data/kali-docker/ultra-only-probe-20261112.json`。

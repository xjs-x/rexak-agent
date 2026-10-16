# 命令执行底座开发自测报告

本报告与[本轮验收记录](../2026-10-16/execution-runtime/README.md)、[64 项证据索引](../2026-10-16/execution-runtime/matrix.md)配套。开工基线 `a3d9df06fa804374fc24c77c5f2140fe7e79146c`，分支 `codex/implementation-v1`；既有部分实现继续补齐，最后集中一次提交，不 push。

## 归属与保存

开工 tracked patch 和 untracked SHA 清单保存在本机临时目录 `rexak-execution-baseline-tuy94oko`。既有 `.workbuddy/memory/**`、`docs/experiments/002-v1-three-targets-2026-10-13/**`、`tests/integration/test_web_model_settings.py` 不进入本次提交。`routes.py` 的 Cookie 复用、`client.ts` 的认证重试及模型设置写入属于用户既有修改，保留在工作区，通过逐段暂存排除。

验证运行于保留这些既有改动的工作区，不能将它们当成本次提交新增能力。`source-manifest.json`明确标记暂存内容与工作区存在差异的共享文件；保护性核对见 `preserved-user-changes.json`。未更改旧 `C:\xjs\xak-agent`。

## 实现范围

协议真源为 `contracts/execution_runtime.py`，执行端使用生成副本，前端使用生成 OpenAPI 类型。Executor 持有执行事实及输出；控制端业务表存资源投影、委托、等待、事件收据和交付；官方 LangGraph checkpoint 保持图状态。没有第二套 Agent 主循环。

独立 job_worker、PTY、输出存储、watch、文件/环境与模板实现位于 Executor/tools；工具派发、fence、取消、阶段结束判定、可靠事件交付接入既有 Runner；ExecutionPanel 提供资源及值班操作。新增 PG 迁移 `e5a1c3d7b902` 与可重复 SQLite 升级，保留旧工作记录。独立 wheel 使用锁定并带哈希的依赖安装。

## 检查方法

以下命令在仓库根运行；最终退出码、计数及运行耗时以 `local-checks.json` 和原始日志为准。

```powershell
uv sync --frozen --all-packages --group dev
uv run python scripts/generate_executor_contracts.py --check
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration tests/recovery -m "not real_model" -q --tb=short
uv run pytest executor/tests -m "not real_model" -q
uv run python scripts/export_openapi.py web/openapi.json
pnpm --dir web install --frozen-lockfile
pnpm --dir web generate:api
pnpm --dir web typecheck
pnpm --dir web build
git diff --cached --check
```

Windows 不具备的真进程/PTY 能力在 Kali 专属临时目录、随机回环端口、独立数据根和私有安装包中补验。完整 Executor 批次 92 passed；补充 11 passed 与其有重叠。Windows 早期批次为 41 passed / 56 skipped，不将 skip 算作通过；新增测试的 Linux 结果在扩展日志中。

PostgreSQL 集成及恢复测试使用独立测试数据库。关键修复后重新跑完整控制端回归；`real_model` 显式排除，缺少凭据的旧测试跳过也分别计数。

## 复现部署演练

`scripts/accept_execution_runtime.py`支持 `isolated`、`deployed-readonly`、`deployed-smoke`，见其 `--help`。isolated 在 Linux 创建自己的服务与数据，可故障注入；已部署模式不触碰既有用户 session，smoke 只处理本次 fixture 资源。`scripts/accept_execution_runtime_ui.py`提供资源面板确定性演练。

实际部署由 `scripts/deploy_executor.py`完成：本机构建 wheel，按现有 SSH/known_hosts 上传、校验并安装，再使用已支持的个人控制脚本重启，核对源码、导入路径、依赖、身份及能力。没有向 Executor 提供模型 Key 或控制端数据库凭据。原入口以 `start-workbench.cmd --api-only --no-browser --smoke-seconds 5`复核，避免用户待执行任务触发真实模型。

受限回退先将本仓库开工基线的 `executor/src`导出到独立目录，然后在 Linux 运行 `python scripts/accept_execution_runtime_rollback.py --legacy-source <导出目录>/executor/src --output <证据目录>`。它只创建自身 fixture，旧版不接管新执行协议；恢复新版后原后台任务仍可核对、读日志及确认取消。此次实际运行通过，结果及基线源码哈希在 `rollback/`。没有将 SQLite 快照直接覆盖回仍有活跃写入的日常数据库。

## 判定边界

所有已发现失败均修复后复验，原失败证据保留。H06 在隔离 fixture 中补齐受限旧二进制回退/新版恢复；备份/隔离还原和增量升级通过。真实模型、真实目标、日常服务生产降级及机器重启自动拉起未测试。`accounting_only`不等于硬配额，`trusted`不等于系统级网络隔离，PTY 不承诺跨服务重启重新接回输入输出或活跃会话降级。

最终代码和证据清单在本轮目录的 manifest 中，不在报告内写入自身最终 commit SHA。用户既有认证修改、实验资料和 memory 保持未提交。

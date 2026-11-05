# 部署、启动与更新

首次 clone 或换电脑，请先按[新成员部署指南](team-setup.md)完成个人配置、SSH 和初始化。Kali 地址放在自己的 `.env`，不修改源码；已有环境无需重新生成配置。

## 首次安装

准备 Git、uv、Docker Desktop、Node.js 22.19 或更新版本、与 `web/package.json` 匹配的 pnpm，以及具备 SSH、Python 3.12/3.13、venv 和 Chromium 的 Kali 后：

```powershell
uv sync --frozen --all-packages --group dev
# 将示例 IP 和用户替换为自己的 Kali
uv run python scripts/setup_workbench.py configure --executor-host 192.168.56.101 --executor-user kali
uv run python scripts/setup_workbench.py install
```

configure 只新建配置和独立令牌，不覆盖旧文件；install 完成本机数据库/前端及 Kali 初始化，不启动模型任务。这是 Standard 的首次安装，`install` 不安装或配置 Ultra Pi Worker。SSH 准备步骤见[完整指南](team-setup.md)。

## 日常启动

保持 Docker Desktop 和 Kali 运行，双击 `start-workbench.cmd`。已有桌面 **Rexak Agent** 快捷方式可继续使用。模型和 Key 在 Web“模型设置”保存，不在终端输入。配置了 Ultra Worker 时，入口会在 Runner 前启动并检查 Worker；服务不可用时明确报错。

控制台：<http://127.0.0.1:8770/console/>。保留启动窗口，按 `Ctrl+C` 停止本次 API、Runner 和由入口启动的 Worker；数据库和 Kali Executor 保留运行。

只检查环境，不启动 API/Runner：

```powershell
uv run python scripts/start_workbench.py --check-only
```

## 启用 Ultra

先完成首次安装，启动 Standard 控制台并保存 Main 所需的模型设置和 Key，再停止启动窗口。在仓库根目录安装 Worker 依赖、构建服务并创建独立配置：

```powershell
npm ci --prefix worker
npm run build --prefix worker
uv run python scripts/start_ultra_worker.py --configure
uv run python scripts/start_ultra_worker.py --configure-model --provider bailian --model deepseek-v4.1-flash
.\start-workbench.cmd
```

示例的供应商和模型需按自己已保存的设置替换；`--configure-model` 需要对应供应商的 Key 已在 Web 中保存。`--configure` 创建仅访问 `ultra_worker` 台账的数据库角色和本机加密服务凭据，已有配置会保留。Worker 模型配置是独立快照；Main 设置变化后需重新运行 `--configure-model` 并重启。未配置该快照时，Worker 使用 Mock，不用于真实模型测试。启动入口使用本机回环端口检查 Worker 的鉴权健康状态和 A2A 身份，日志在 `logs/launcher/worker.log`。

## 更新代码

先停止启动窗口并核对活动任务，再在项目根目录执行：

```powershell
git pull
uv sync --frozen --all-packages --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run workbench db-init
```

已启用 Ultra 的环境还需在停止旧启动窗口后更新 Worker 的本机依赖和构建产物，再启动新版本：

```powershell
npm ci --prefix worker
npm run build --prefix worker
```

保留原 Worker 服务凭据和独立模型快照；需要更新 Worker 使用的供应商、模型参数或 Key 时，重新运行 `--configure-model` 并重启。旧 API 或 Worker 进程仍在运行时，源码和构建产物更新不表示它们已加载新版。

Executor 有变化时构建并部署；脚本保留配置与数据，核对文件哈希后重启。有活动任务时停止更新：

```powershell
uv build --wheel --package workbench-executor --out-dir data/tmp/deployment
uv run python scripts/deploy_executor.py --wheel data/tmp/deployment/workbench_executor-0.1.0-py3-none-any.whl
```

包版本变化时使用构建命令实际输出的 wheel 文件名。首次部署使用安装器，或给部署命令加 `--bootstrap`创建缺失的专用 venv；系统依赖仍需事先准备。完成后重新运行 `start-workbench.cmd`。

移动仓库时，检查 `.env`中的绝对数据/备份路径。未设置时默认跟随当前仓库，需要自行迁移原数据。

## 查看错误

- 初始化日志：当前数据目录 `logs/setup/`。
- 启动日志：当前数据目录 `logs/launcher/`。
- Kali 日志：安装目录下的 `executor.log`，默认 `~/rexak-agent-executor/executor.log`。

只停止数据库使用 `docker compose stop postgres`。不要将 `docker compose down -v`当作日常操作，它会删除数据库卷。


## v1.0 安全切换与独立验收

当前版本移除旧 passive/Shadow/Progress 配置与工具，不需要配置关闭这些功能。
历史模式读取兼容，不覆盖 `.env`；Decision Memory 的现有 memory/off 与运维 force_off 保留。

更新前先核对 API/Runner 进程、非终态 Run、有效租约、Job 及开放资源。停止新任务提交；
有无法确认的执行状态或历史队列时，先在原版本按运行控制完成暂停/对账，不要启动会自动领取历史队列的 Runner。
不要靠清库、清 STOP、改 Run 状态或删除队列完成更新。

1. 保存当前源码与配置备份，使用 `uv run workbench backup --docker-container workbench-postgres` 备份日常库与工件。
2. 安装锁定依赖，构建前端；修改 `.env.example` 不代表要覆盖已有 `.env`。
3. 在维护窗口执行 `uv run workbench db-init`。新迁移恢复资产身份唯一约束；有重复历史键时事务回滚，保留数据并调查，不自动删行。
4. 先用 `start-workbench.cmd --api-only` 或 `uv run workbench api` 验证健康、登录、历史聊天、图和工件读取。
5. 明确选择本次允许继续的任务后再启动 Runner。CLI 支持的 `--run-id`、`--once`、`--no-recover` 以 `uv run workbench runner --help` 为准；不要把不扫描恢复误当成不领取 queued。
6. Executor 文件相同且身份/能力一致时保留原进程；确有更新才按现有部署脚本检查活动作业、安装和核对新 boot_id。

需要独立验证时设置专用 `WORKBENCH_DATABASE_URL`、`WORKBENCH_DATA_ROOT`、`WORKBENCH_API_PORT`，
使用无历史队列的隔离库。API 和 Runner 必须共用同一隔离配置。不要向日常实例注入验收任务。
测试脚本与 Mock 模型只能证明协议、持久化、恢复和安全边界，不能证明真实靶场效果。

API/Runner 重启加载新代码；停止启动窗口不会清除 Checkpoint、任务或工件。旧节点挂起的退役模式不再继续执行，
只读查看历史不要求重新启用旧检测器。原始记录保留，必要的任务迁移应单独维护处理。

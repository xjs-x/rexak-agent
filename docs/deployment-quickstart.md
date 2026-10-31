# 部署、启动与更新

首次 clone 或换电脑，请先按[新成员部署指南](team-setup.md)完成个人配置、SSH 和初始化。Kali 地址放在自己的 `.env`，不修改源码；已有环境无需重新生成配置。

## 首次安装

准备 Git、uv、Docker Desktop、Node.js/pnpm，以及具备 SSH、Python 3.12/3.13、venv 和 Chromium 的 Kali 后：

```powershell
uv sync --frozen --all-packages --group dev
uv run python scripts/setup_workbench.py configure --executor-host <自己的Kali地址> --executor-user <SSH用户名>
uv run python scripts/setup_workbench.py install
```

configure 只新建配置和独立令牌，不覆盖旧文件；install 完成本机数据库/前端及 Kali 初始化，不启动模型任务。SSH 准备步骤见[完整指南](team-setup.md)。

## 日常启动

保持 Docker Desktop 和 Kali 运行，双击 `start-workbench.cmd`。已有桌面 **Rexak Agent**快捷方式可继续使用。模型和 Key 在 Web“模型设置”保存，不在终端输入。

控制台：<http://127.0.0.1:8770/console/>。保留启动窗口，按 `Ctrl+C`停止本次 API 和 Runner；数据库和 Kali Executor 保留运行。

只检查环境，不启动 API/Runner：

```powershell
uv run python scripts/start_workbench.py --check-only
```

## 更新代码

先停止启动窗口并核对活动任务，再在项目根目录执行：

```powershell
git pull
uv sync --frozen --all-packages --group dev
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run workbench db-init
```

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

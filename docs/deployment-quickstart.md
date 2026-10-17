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

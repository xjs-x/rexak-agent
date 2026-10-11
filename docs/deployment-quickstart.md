# 本机部署与启动

当前 Windows 和 Kali 已配置完成。Kali 执行端地址：`192.168.72.134`。

## 日常使用

1. 保持 Docker Desktop 和 Kali 虚拟机运行。
2. 双击桌面的 **Rexak Agent**；也可双击仓库中的 `start-workbench.cmd`。
3. 输入小米 API Key，等待自动打开控制台。
4. 直接在聊天里发送目标网址或 IP 和测试要求；默认按指令运行，无需配置 scope。
5. 暂停后直接发送新指令即可继续，也可以点击“新聊天”开展另一任务。

启动窗口会自动检查数据库、启动 Kali Executor、检查数据库版本、启动 API 和 Runner。Key 仅本次使用，不写入文件，也不上传到 Kali。

控制台地址：<http://127.0.0.1:8770/console/>。保留启动窗口，按 `Ctrl+C` 停止本次启动的 API 和 Runner。

## 命令行启动

```powershell
cd C:\xjs\rexak-agent
.\.venv\Scripts\python.exe scripts/start_workbench.py
```

只检查部署状态：

```powershell
.\.venv\Scripts\python.exe scripts/start_workbench.py --check-only
```

## 更新代码后

先在启动窗口按 `Ctrl+C`。更新控制端依赖与前端（需要 uv、pnpm 在命令行可用）：

```powershell
cd C:\xjs\rexak-agent
uv sync --frozen --all-packages --group dev
cd web
pnpm install --frozen-lockfile
pnpm build
cd ..
```

Executor 代码有变化时，构建并上传；脚本会检查活动任务、验证文件哈希、安装并重启执行端：

```powershell
uv build --wheel --package workbench-executor --out-dir data/tmp/deployment
.\.venv\Scripts\python.exe scripts/deploy_executor.py --wheel data/tmp/deployment/workbench_executor-0.1.0-py3-none-any.whl
```

完成后再次双击 **Rexak Agent**。

## 查看启动错误

本机日志：`C:\xjs\rexak-agent\data\logs\launcher\`。

Kali 日志：

```bash
tail -n 50 ~/rexak-agent-executor/executor.log
```

需要停止数据库时：

```powershell
cd C:\xjs\rexak-agent
docker compose stop postgres
```

完整容器化和网页配置向导尚未实现；当前双击入口使用已配置的本机环境。不要运行 `docker compose down -v`，它会删除数据库卷。

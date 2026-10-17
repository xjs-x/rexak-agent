# 新成员部署与协作

每位开发者 clone 同一个仓库，使用自己的 Windows 控制端、PostgreSQL 和 Kali。Kali IP、SSH 密钥、令牌及数据目录都放在个人 `.env`，不修改源码，也不分发原作者的运行目录或虚拟机。

验证状态见[本轮记录](acceptance-results/2026-10-17/team-setup/README.md)。已在现有 Kali 的全新独立目录验证首次安装、重复安装与启动器检查；新成员仍需先准备下述系统依赖及 SSH，不包含自动安装虚拟机或系统软件。

## 1. 准备环境

Windows 先准备 Git、uv、Docker Desktop、OpenSSH 客户端、Node.js 22 LTS，以及与 `web/package.json` 的 `packageManager` 一致的 pnpm。Python 3.12 由 uv 按 `.python-version`管理。Docker Desktop 和 Kali 需要运行，Windows 必须能访问 Kali 的 SSH 端口。

```powershell
git clone <GitHub仓库地址> rexak-agent
cd rexak-agent
# 如果团队使用非默认分支，在这里 git switch <分支名>
uv sync --frozen --all-packages --group dev
```

先确认团队约定分支/版本；维护者尚未推送的修改不会出现在 clone 中。项目可以放在任意目录，包括带空格和中文的路径；默认数据和备份目录跟随当前仓库。

在 Kali 终端准备系统依赖，需要该成员自己的 sudo 权限：

```bash
sudo apt update
sudo apt install -y openssh-server python3-venv chromium
sudo systemctl enable --now ssh
python3 --version
hostname -I
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Executor 要求 Python **3.12 或 3.13**。如果系统 `python3`是其他版本，需先安装兼容解释器及对应 venv 支持，再在 `.env`设置 `WORKBENCH_EXECUTOR_PYTHON=python3.13`或该解释器的绝对路径。初始化器不会修改系统 Python、执行 sudo 或自动放宽版本范围。

`hostname -I`用于找到自己的 Kali IP；最后一条命令给出 SSH 主机指纹，供下一步核对。

## 2. 配置个人 SSH

已有可连接 Kali 的密钥可以复用。没有时，在 Windows PowerShell 创建自己的密钥；已有同名文件时选择新文件名，不要覆盖：

```powershell
ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519"
```

以下 IP 和用户名替换成自己的，将**公钥**安装到 Kali：

```powershell
Get-Content "$HOME/.ssh/id_ed25519.pub" | ssh kali@192.168.56.101 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys'
ssh -o BatchMode=yes -i "$HOME/.ssh/id_ed25519" kali@192.168.56.101 true
```

首次连接时，对照 Kali 上的主机指纹确认主机；OpenSSH 会将其保存到 `~/.ssh/known_hosts`，初始化器不自动信任未知主机。第二条命令应成功且不回退到登录密码；初始化器不会交互索取 SSH 密码或私钥口令。

非 22 SSH 端口需给以上 SSH 命令加 `-p <端口>`，并给下一步 configure 加 `--ssh-port <端口>`。

## 3. 生成个人配置

在仓库根目录运行，填写自己的 Kali 地址和 SSH 用户：

```powershell
uv run python scripts/setup_workbench.py configure --executor-host 192.168.56.101 --executor-user kali
```

使用其他密钥或 known_hosts 文件时，可改用以下示例（两条二选一执行）：

```powershell
uv run python scripts/setup_workbench.py configure --executor-host 192.168.56.101 --executor-user developer --ssh-key "C:/Users/Developer Name/.ssh/rexak_ed25519" --known-hosts "C:/Users/Developer Name/.ssh/known_hosts"
```

脚本从 `.env.example`创建 `.env`，为本地会话、访问和 Executor 分别生成随机令牌，不打印令牌。已有 `.env`时拒绝覆盖，不替换现有凭据；`.env`已被 Git 忽略。

| 字段 | 作用 |
|---|---|
| `WORKBENCH_EXECUTOR_HOST` / `USER` / `PORT` | 自己的 Kali 地址、SSH 用户和端口 |
| `WORKBENCH_EXECUTOR_SSH_KEY` / `KNOWN_HOSTS` | 自己的私钥和主机校验文件 |
| `WORKBENCH_EXECUTOR_INSTALL_ROOT` | 默认 `~/rexak-agent-executor`；按 SSH 用户 home 展开 |
| `WORKBENCH_REMOTE_DATA_ROOT` | 默认 `~/rexak-agent-data`，与安装目录分离 |
| `WORKBENCH_EXECUTOR_PYTHON` | Kali 创建 venv 使用的兼容 Python 可执行文件 |
| `WORKBENCH_EXECUTOR_PIP_INDEX_URL` | 可选公开 HTTPS Python 包源；留空沿用 Kali pip 配置，不填写凭据或查询参数 |
| `WORKBENCH_DATA_ROOT` / `BACKUP_PATH` | 可选；不配置时使用当前仓库 `data/` / `backups/` |

不要把模板中注释的可选目录填写为空值。更换 Kali IP 只需修改自己的 `.env`并重启启动窗口，不需修改源码或重装 Executor。更换用户名/虚拟机则要重新准备该机器的 SSH 和安装环境。

## 4. 首次安装

```powershell
uv run python scripts/setup_workbench.py install
```

脚本依次启动本地 PostgreSQL、迁移数据库、安装并构建前端、构建 Executor wheel、通过 SSH 创建缺失的专用 venv、安装依赖和 wheel、启动及核对 Executor，最后执行启动器 `--check-only`。它不启动 Runner，不调用模型，不访问测试目标。

安装器在**指定 Kali**启用 `trusted`命令执行，不逐条审批命令；这不是系统级网络隔离。模型 Key 和数据库凭据不会上传给 Executor。Kali 需要能从配置的包源下载 Python 依赖，这不是完全离线安装。浏览器使用已安装的系统 Chromium。

默认 PyPI 下载很慢时，可以在自己的 `.env`设置公开包源，例如 `WORKBENCH_EXECUTOR_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，也可在 configure 时加 `--pip-index-url <HTTPS地址>`。这个配置仅传给本次依赖安装，不修改 Kali 的全局 pip 配置；不支持把包源密码或令牌放在此字段，私有包源认证应在 Kali 自行管理。

既有配置、数据和运行记录会保留。令牌/身份/端口不一致、数据库有活动租约或执行端有活动任务时停止更新，不强行清理。重复安装重新构建及核对，不重新生成令牌；不完整的远端 `.venv`会报错，不自动删除。

步骤日志位于当前数据目录的 `logs/setup/`，安装证据为 `logs/setup/executor-deployment.json`。修正失败原因后可重跑 install；不要通过删除数据卷解决安装问题。

## 5. 启动与开发

```powershell
.\start-workbench.cmd
```

控制台：<http://127.0.0.1:8770/console/>。每位成员在 Web“模型设置”保存自己的供应商、端点和 Key；Windows 使用当前用户加密存储，不复制别人的密钥文件。

前端开发可另开终端运行 `pnpm --dir web dev`。API/Runner 仍由统一启动入口管理。SSH 隧道访问 Kali 回环地址的 Executor，无需把 Executor HTTP 端口开放到局域网。更新见[部署指南](deployment-quickstart.md)。

## 6. 协作边界

提交源码、锁文件、迁移和公共文档；不提交 `.env`、私钥、模型 Key、数据库、`data/`、`backups/`、`.venv/`或 `node_modules/`。提交前检查 `git status --short`。

每人运行独立环境，不共享本机数据库或复制原作者数据。目标地址的 `localhost`指执行请求的那台机器；目标在 Windows Docker 时，Kali 的 `127.0.0.1`不会自动指向 Windows，需按[目标映射说明](p3-p5-deployment.md)配置可达地址。

目前支持 Windows 控制端 + 独立 Kali 的安装流程，不是容器化全栈或网页安装向导。

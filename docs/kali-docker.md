# Kali Docker Executor

Main、API、Runner、Pi 保留在宿主机；PostgreSQL 使用原 compose.yaml。
Kali 官方基础镜像的系统 Python 供发行版工具使用，Executor 使用独立 Python 3.13。
Dockerfile 固定 Kali 基础镜像摘要；构建时 apt 软件包仍来自 rolling 仓库。

## 首次构建与启动

在仓库根目录执行（Windows 使用 uv，也可用已有 .venv 的 Python）：

```powershell
uv run python scripts/kali_docker.py configure
uv run python scripts/kali_docker.py build
uv run python scripts/kali_docker.py up
.\start-workbench-docker.cmd
```

日常直接双击原入口 `start-workbench.cmd`，它默认使用 Kali Docker；`start-workbench-docker.cmd` 是同一入口的别名。
Linux 使用 `uv run python scripts/start_workbench.py --executor-docker --no-browser`。
原 `.env` 中的虚拟机配置保留，原入口不会改写它。
切换前先在原窗口正常停止控制端，确认活动任务；不得同时运行两套 Runner 领取同一队列。

只管理 Kali：

```powershell
uv run python scripts/kali_docker.py status
uv run python scripts/kali_docker.py stop
docker compose -f compose.yaml -f compose.kali.yaml logs kali
```

镜像源网络不通时可显式选择 Kali 镜像站，不关闭 apt 签名检查：

```powershell
docker compose -f compose.yaml -f compose.kali.yaml build --build-arg KALI_MIRROR=http://mirrors.qlu.edu.cn/kali kali
```

## 身份、持久化与限制

SSH 发布到宿主机 127.0.0.1:2222；Executor HTTP 只监听容器 127.0.0.1:8791。
专用客户端密钥、authorized_keys、Executor token 位于被 Git 忽略的 data/kali-docker。
主机公钥从本地 Docker 引擎取得并固定；已记录身份变化会拒绝连接，不自动覆盖 known_hosts。
私钥与该目录应保留在当前用户的受限账户目录；迁往 VPS 后应设置目录 0700、私钥/令牌 0600。

workbench-kali-data 卷保存 SQLite、jobs（输出/回执）、Session/Worker 工作区、PTY 日志和浏览器状态。
workbench-kali-hostkeys 卷保存 SSH 主机密钥。Executor 服务日志写数据卷的 executor-service.log；SSHD 日志使用 Docker 日志管理。
禁止日常运行 down -v。备份应覆盖两卷和独立客户端凭据，不能只备份 PostgreSQL。

容器重启会结束其中的进程；持久文件和已完成回执可用于原 Executor 对账。
进行中的命令无可靠完成回执时可能标 unknown，不能承诺无损恢复；不会自动重放未知副作用。
普通服务进程重启与容器重启的语义不同，Pi 仍遵循其已有中断恢复规则。

Executor 以 kali 用户运行；SSHD/引导监督进程以 root 运行。
默认无 privileged、Docker socket、宿主 PID namespace 或额外 capability。
512 MiB /dev/shm 用于 Chromium。工作区分目录是逻辑隔离，trusted 模式不是系统安全沙箱。
安装基础 shell/HTTP、nmap/sqlmap、gcc/binutils/gdb/checksec/patchelf、系统 pwntools/ROPgadget 和 Chromium。
不包含完整 Kali 工具集、原虚拟机的个人安装、GEF、桌面 GUI 或 VPN 配置。
调试自己创建的子进程可以测试；附加其它进程、原始网络能力、内核/设备相关工具须按实际需求另验。

VPS 需支持 Docker/Compose、出站依赖下载和题目网络；浏览器与多 Worker 并发所需资源应实测。
本配置不向执行端注入模型 Key、数据库凭据或 CTFy token。

## 本地验收（2026-11-12）

最终镜像已真实构建并启动，容器 health 为 healthy。
Windows 专用密钥登录、严格 known_hosts 校验（含错误主机密钥拒绝）、SSH 隧道与 HTTP API 通过。
已核对镜像中 Executor Python 源码哈希与工作区一致。
真实测试通过：Shell/Python、上传与读回、curl/HTTP/Artifact 下载、并发 Session 目录隔离、
后台命令取消、PTY、Chromium/Playwright、本地编译程序的 GDB 子进程调试及所列工具启动。
容器重启后工作区、SQLite 记录、服务日志前缀和 HTTP Artifact 保留；中断命令为 unknown，未自动重跑。

使用独立 PostgreSQL 测试容器进行的两项调用链验收通过：

- 原 API 创建 Session → AgentRunner → 生产 Main 图/命令工具 → SSH/HTTP → Docker Executor → 工具结果与数据库记录。
- 官方 create_agent Main → A2A → 原 Pi 循环/工具代理 → 原 API 工具内核 → SSH/HTTP → Docker Executor → Artifact/候选汇报/续用历史。

这两项仅使用确定性模型响应，不证明真实模型解题效果；没有请求付费模型或赛事靶机。
原 CMD 加 --executor-docker 的 API/Worker 启动也在独立数据库验收，未扫描日常恢复队列。
日常库历史 Run 的逐行哈希在验收前后比较；验收证据保存在被 Git 忽略的 data/kali-docker 下。

重复运行低层验收前须确保 Kali 容器空闲；重启选项会中断该容器的活动进程：

```powershell
uv run python scripts/accept_kali_docker.py --restart-container
```

生产日常入口会按原规则恢复可恢复任务。只查看历史时使用
`start-workbench.cmd --executor-docker --api-only --no-recover`。
以上验收未对原 Kali 虚拟机操作；原 .env 与比赛凭据保持原位置。

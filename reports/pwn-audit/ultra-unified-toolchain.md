# Ultra 统一内置工具环境接入

日期：2026-11-08（北京时间）。**源码修复、同机隔离执行验收通过；生产尚未加载。** 生产仍有活动 PTY，本次没有更新其已安装源码、命令目录或重启服务。没有运行全量回归、真实 CTF、目标扫描或付费模型，没有 commit/push。

## 1. 实现方案

Ultra 继续使用现有 Main、Pi Worker、bash RPC 和 Executor。没有增加题型模式、profile、任务选择器、关键词判断、Function Calling 接口或工具 Schema。

Executor 在统一最小命令环境构造函数中，将 `<EXECUTOR_DATA_ROOT>/tools/bin` **追加到原 PATH 末尾**。该目录不存在时保留旧行为；重复构造环境不会重复追加。原有命令优先级保持不变，不读取用户 shell profile，不运行 `activate.sh`。

维护时将已安装并验收的工具链注册为六个普通命令：

|命令|实际指向 / 行为|
|---|---|
|`gdb`|现有 `pwn-20261108/bin/gdb` 包装器，保留其独立库与 GDB Python 运行时|
|`gef`|同一个 GDB，加显式 `-ex 'source .../sysroot/usr/share/gdb/gef.py'`；不写 `.gdbinit`|
|`checksec`|现有官方包解包的 checksec 入口|
|`ROPgadget`|现有专用 venv 的 ROPgadget 入口，使用原 shebang|
|`patchelf`|现有独立 patchelf 包装器|
|`pwn-python`|直接 exec 已安装工具链的 `venv/bin/python3`，传递全部参数|

这是一份部署级命令目录，不是任务配置；Web、Pwn、Reverse、Crypto 任务均使用同一个环境。Main/Worker 不必声明题型或激活 venv，例如：

```bash
nmap --version
sqlmap --version
checksec --file=./fixture
ROPgadget --binary ./fixture
pwn-python analysis.py
gdb -nx -batch -ex 'break main' -ex run ./fixture
gef -nx -batch -ex 'break main' -ex run -ex context ./fixture
```

环境发现与命令执行使用同一个 PATH 构造规则，默认清单加入新增入口，并在既有 `dependency_environment_hint` 中说明 `pwn-python` 用法。未改 System Prompt 或该返回值的 Schema。GEF 入口只报告路径及“版本未核实”，避免将 GDB 的 `--version` 错当成 GEF 版本；实际 GEF 可用性由执行验收确认。

维护安装器只注册已经存在且可执行的工具，不下载软件，不修改系统 Python 或 Executor venv。注册前检查全部入口，已有不同内容或符号链接时保留并报错；相同内容可重复执行。没有注册 `python`、`python3`、`pip`、Nmap、Nuclei、Sqlmap 等同名覆盖入口。

## 2. 修改文件

|文件|改动|
|---|---|
|`executor/src/workbench_executor/processes.py`|统一 PATH 追加规则；旧 ProcessSupervisor 也可复用同一目录|
|`executor/src/workbench_executor/settings.py`|从现有 data_root 派生固定 `tools/bin` 属性，没有新增题型或环境配置字段|
|`executor/src/workbench_executor/api.py`|将派生目录传给旧命令执行器|
|`executor/src/workbench_executor/command_runtime.py`|独立 Job 包装进程使用统一环境|
|`executor/src/workbench_executor/job_worker.py`|实际命令、脚本、监视 Job 在已有最小环境中加入同一目录|
|`executor/src/workbench_executor/terminals.py`|PTY 使用同一环境构造规则|
|`executor/src/workbench_executor/execution_environment.py`|按实际执行 PATH 定位和探测工具；复用原环境结果结构|
|`executor/src/workbench_executor/builtin_commands.py`|维护级入口注册逻辑与 CLI，随 Executor 包交付|
|`scripts/install_builtin_commands.py`|调用上述模块的薄维护入口|
|`executor/tests/test_builtin_commands.py`|保护 PATH 优先级、Python 环境、Worker HOME/TMPDIR、安装幂等与已有文件|
|`docs/implementation-status.md`|记录隔离验收与生产待部署边界|
|本报告|部署、兼容性及验收证据|

本次没有修改 Main/Pi 循环、A2A、委派状态、Finding、历史持久化、权限、fence、超时策略、网络控制或数据库结构。原有控制端和 Worker 的未提交修改保留。

## 3. Web / Python 兼容性审查

安装前按生产 PATH 检查了真实路径、文件 SHA256、shebang 与启动结果：

|原工具|启动方式与安装前结果|统一环境验收|
|---|---|---|
|Nmap|`/usr/bin/nmap`，shell 入口；7.99|Main/Worker `--version` 均退出 0|
|Nuclei|`/usr/bin/nuclei`；v3.8.0|Main/Worker `-version` 均退出 0，不扫描或更新模板|
|Sqlmap|`/usr/bin/sqlmap` → `/usr/share/sqlmap/sqlmap.py`；shebang `#! /usr/bin/python3`；1.10.6#stable|Main/Worker `--version` 均退出 0|
|httpx|`/usr/bin/httpx`，shebang `/usr/bin/python3`，是 Python HTTP 客户端|Main/Worker `--help` 均正常启动|
|默认 Python|`/usr/bin/python3`，Python 3.13.12，`sys.prefix=/usr`|路径、prefix、sys.path、requests/urllib3 来源保持一致|
|Executor Python|`/home/kali/rexak-agent-executor/.venv/bin/python`，3.13.12|生产 venv 未安装、升级或改写任何依赖|

安装前误用同名扫描器参数 `httpx -version` 返回 2，提示不支持该选项；这确认当前命令是 Python HTTP 客户端，不能据此报告扫描器缺失或新增回归。实际兼容性验收改用此工具自己的 `--help`，没有替换它。

系统 Python 的 requests/urllib3 来自 `/usr/lib/python3/dist-packages`，系统没有 pwn/elftools；Executor venv 也没有这些 Pwn 包。直接把 Pwn venv/bin 前置会改变 `#!/usr/bin/env python3` 的解析及普通 Python 脚本依赖，所以采用独立命令别名：

- 不向通用 PATH 加入 Pwn venv/bin，也不设置通用 `VIRTUAL_ENV / PYTHONHOME / PYTHONPATH / LD_LIBRARY_PATH`。
- 默认 `python3` 仍解析 `/usr/bin/python3`，原 `/usr/bin/python3` shebang 和 env-python3 shebang 均保留原行为。
- 只有显式执行 `pwn-python` 的脚本使用 Pwn venv，pwntools 4.15.0、pyelftools 0.33、ROPgadget 7.7 保持原安装。
- GDB 原包装器的库路径仅存在于其子进程内，不污染后续 bash 命令；验证流程在 GDB/GEF 之后再次运行默认 Python 脚本成功。
- PATH 尾部追加保留已有同名系统命令的优先级；将来若系统另行安装同名工具，应重新验收实际选中的版本。

## 4. 实际执行验收

### 运行隔离与源码身份

Kali 新建维护目录：

`/home/kali/rexak-agent-data/maintenance/unified-30dde645e0fb`

使用独立候选源码、Python venv、随机回环端口、fixture token、数据根和台账启动真实 Executor。候选 venv 通过只读 site 路径复用已有 Executor 依赖，不修改生产 venv；独立 Job 也由该候选解释器启动，未意外回落到生产旧模块。

命令注册到该实例的 `isolated-data/tools/bin`，实际底层工具仍使用既有 `/home/kali/rexak-agent-data/toolchains/pwn-20261108`。所有执行命令均没有 source/activate，也没有修改 bash PATH。最终候选包 **27 个 Python 文件与本机逐项 SHA256 相同**。

### 五个实际执行场景

|场景|执行证据与结果|
|---|---|
|Main Web→Pwn 共存|同一笔 bash 先启动 Nmap/Nuclei/Sqlmap/httpx，检查默认 Python 与 env-python3 shebang 脚本，再编译本地无害 ELF、静态分析、Pwn Python 交互、GDB 与 GEF，最后再运行原 Python 脚本；退出 0，`COEXISTENCE_OK`|
|Worker Web→Pwn 共存|同样完整顺序，在真实 `worker_workspace_id` + `delegation_id/revision=1` 下执行；退出 0，`COEXISTENCE_OK`|
|旧参数普通 bash|没有新增工具环境参数的旧信封运行 `printf ...; exit 7`；真实资源退出码 7，没有掩盖失败|
|Worker GDB 超时|直接 `gdb -nx -batch -ex 'python ... sleep(30)'`，单操作超时 2 秒；记录 `execution_timeout`、退出码 1、`group_confirmed_exited=true`|
|PTY|真实 terminal_open/send；直接执行 pwn-python 导入及 GDB Python 命令，普通 python3 仍为 `/usr/bin/python3`；终端正常退出 0|

Main 的 cwd 为隔离 Session 工作区，HOME 保持 `/home/kali`；Worker cwd 为该 Session 下 `.rexak/workers/705ed2af-ca6e-421c-b166-15ec1e4e2412`，HOME/TMPDIR 分别仍为 Worker 的 `.home/.tmp`。没有借助共享 HOME 激活环境，Session 和授权信封继续沿用原机制。

Main 和 Worker 均实际完成：

- readelf Header/符号表与 objdump 反汇编；checksec 输出 Full RELRO、Canary、NX、PIE；ROPgadget 找到本地 fixture gadget；patchelf 读出动态解释器；radare2 读取 ELF 基本信息。
- `pwn-python` 导入 pwntools/pyelftools，读取 ELF 结构与保护，启动本地回显进程，发送 `UNIFIED`，收到 `ECHO:UNIFIED:42` 并确认退出 0。
- 裸 `gdb` 命中 marker 断点，参数为 41，读取 RIP/RSP、调用栈和全局内存值 41，继续后正常退出。
- 裸 `gef` 自动加载已安装 GEF，命中同一无害程序断点，输出 registers/stack/code/threads/trace 上下文；没有模型手动 source。

上述普通 Job 的完成回执均核对进程组退出证据；最后检查候选维护目录相关进程，无残留。仅测试本地自建程序，没有连接题目服务或构造利用。

### 定向测试与本机检查

|检查|结果|
|---|---|
|本机新命令环境与既有分析测试|10 passed / 4 Linux-only skipped；Linux 分支由下面的 Kali 测试验证|
|Kali 定向 Executor 测试|17 passed，包含新环境/安装保护、原秘密过滤与分析检查、旧命令真实退出码、进程组超时及交互 PTY|
|维护安装逻辑归入 Executor 包后的最终核对|4 passed；这是上述安装保护用例的最终版本复查，不累加成 21 项不同测试|
|控制端 Worker 定向集成 Mock|2 passed，临时独立 PostgreSQL 库，覆盖 pending/cancel/路径与超时/unknown 区分|
|Pi remote-tools Mock|8 passed，不调用真实模型|
|修改文件 Ruff / Pyright|通过；仅检查相关文件|
|UTF-8 编码|默认范围 784 文件通过；本报告与本次维护辅助脚本另按相同规则通过，无 BOM、替换字符或乱码|
|本机依赖|`uv sync --frozen --all-packages --group dev` 通过，111 包核对|
|前端构建|通过；保留原有 chunk 大小提示，没有为此重构|
|原桌面入口|快捷方式仍指向 `C:\xjs\rexak-agent\start-workbench.cmd`；现有 `/health` 与 `/console/` 均 HTTP 200，没有重启或触发恢复|

维护函数最初放在脚本内，随后归入 Executor 包，避免执行岛独立测试/安装依赖控制端 scripts 目录。运行环境相关七个文件在真实执行前后未再修改；归入包的维护函数随后重新通过 Kali 四项测试并核对最终源码哈希。

## 5. 生产加载状态与待办

**生产仍运行旧版，本次能力尚未在生产自动启用。** 最后核对：

- executor_id `kali-01`，boot ID `3135933-681aa2e84113`，启动时间 `2026-11-07T03:26:28.293888+00:00`，`active_jobs=1`。
- 活动 PTY 仍为 `4be4256d-ba39-4738-a2d8-60e18a77bae3`，PID `3328015`，预期及实际 start ticks 均 `36836333`。没有停止、重启或接管它。
- 生产普通 Job 计数仍为 cancelled 7 / failed 105 / succeeded 2534 / unknown 22；没有写入生产台账、恢复历史任务或清理 unknown。
- 已安装生产包仍为原 26 文件；与候选差异为七个已修改运行环境文件及新增 `builtin_commands.py`。没有将源码文件写进运行中进程所用目录，避免新子进程混用新旧代码。
- 生产 `/home/kali/rexak-agent-data/tools/bin` 尚不存在；本次只在隔离数据根注册入口。生产 Pi 模型配置没有切换，也没有向生产 Pi 提交任务。

活动资源安全结束后，完整生产加载还需一次受控部署：再次核对所有 Job/PTY → 更新并核验候选 Executor 源码 → 在生产数据根注册同一组六个普通命令 → 启动新实例并核对 boot ID、哈希及真实 bash 环境。注册使用已有维护入口：

```bash
# 仅在后续安全部署阶段执行；本次没有对生产执行。
/home/kali/rexak-agent-executor/.venv/bin/python -m workbench_executor.builtin_commands \
  --data-root /home/kali/rexak-agent-data \
  --toolchain-root /home/kali/rexak-agent-data/toolchains/pwn-20261108
```

这两个参数是维护安装目录，不是模型/任务的环境选项；注册后所有 Main/Worker 自动共享同一组命令。

**未验证范围**：生产新实例加载、真实生产 Pi→控制端→Executor 同笔链路、历史任务实际恢复、真实模型决策/CTF 解题效果、全部 Web 工具及 GEF 插件、跨架构调试。Mock、同机真实执行层与生产模型链路分开记录，不能相互代替。

## 6. 证据位置

- `work/unified-before.log`、`work/unified-web-before.log`：生产资源及 Web/Python 启动审查。
- `work/unified-stage-run.log`、`work/unified-results.json`、`work/unified-environment.json`：五个真实隔离执行场景、完整回执/输出和发现结果。
- `work/unified-kali-tests.log`、`work/unified-local-tests.log`、`work/unified-finalize.log`：定向结果、最终维护函数复查及生产资源保留核对。
- `work/unified-worker-tests.log`、`work/unified-worker-rpc.log`：Worker Mock 分段结果。
- `work/unified-final-hashes.json`：最终候选与保留的生产源码哈希。
- `work/unified-ruff-final.log`、`work/unified-pyright.log`、`work/unified-pyright-provision.log`、`work/unified-encoding.log`、`work/unified-web-build.log`：代码检查、编码与构建。
- Kali 完整源码、独立台账、无害 ELF、spec、原始输出及完成回执保留在上面的 `maintenance/unified-30dde645e0fb` 目录；所有临时执行服务已经退出。

没有修改 iptables、Docker 网络、现有授权、安全控制、系统 Python、生产 Executor venv、已有数据库、凭据和历史测试数据。没有 Git commit/push。

# 真实测试前最终部署验收

更新：2026-11-08。**READY：指定历史 PTY 已安全关闭，Windows 与 Kali 生产版本已更新，统一 Web/Pwn 工具环境已在真实生产 Executor 验收通过，可以由用户开始下一轮真实授权 CTF 测试。**

此前 BLOCKED 结论已由本次生产部署证据取代。没有运行新 CTF、目标扫描、付费模型、全量回归或重复 Mock 测试，没有 commit/push。

## 本次最终结果

|项目|实际结果|
|---|---|
|指定历史 PTY|正常终端关闭接口成功；`process_confirmed_exited=true`、最终 `closed`；原 PID 与进程组已退出|
|其他活动资源|关闭后、部署后及两笔冒烟结束后均无其他活动 Job/PTY；未取消其他资源|
|Kali 同步|原部署流程完整上传 wheel/源码并更新应用包；生产实际安装的 27 个 Python 文件 SHA256 与本机逐项一致，包含 `builtin_commands.py`|
|Kali 真实加载|新 PID 3485002、新 boot ID `3485002-2470199d96ea`；身份 `kali-01`、协议 1，真实生产命令已执行成功|
|六个命令|生产 `tools/bin` 已注册 gdb、gef、checksec、ROPgadget、patchelf、pwn-python；无同名冲突|
|Web/Python 共存|生产 Main bash 启动 Nmap/Nuclei/Sqlmap，通过 Pwn 导入与各命令启动；默认 python3 仍为 `/usr/bin/python3`|
|Worker 环境|生产 Executor Worker 信封执行成功；工具入口与 Main 相同，cwd/HOME/TMPDIR 仍独立|
|Windows API|新 PID 32472，15:21:15 启动，8770 回环监听；health/console/历史只读接口正常|
|Windows Runner|新 PID 3016，15:21:28 启动，原 `workbench_child.py runner --no-recover`；日志确认已启动|
|Pi Worker|新 PID 34880，15:21:24 启动当前 `worker/dist/server.js`，8795 回环监听；health/Card 正常，模型配置未变|
|桌面入口|通过原 `start-workbench.cmd --no-browser --no-recover` 完成冷启动；原快捷方式仍指向本仓库入口，未创建重复实例|
|前端|API 提供的 index/JS/CSS 与当前构建逐字节一致，没有过期源码，不重复构建|
|Main 进度查询|旧控制端进程全部退出，新进程启动晚于最新源码；当前项目模块路径及 `history_cursor/history_limit` 元数据已核对，不再沿用旧进程绑定|
|协作与历史|严格附件交接、visible_events、report_finding 对应当前源码/构建已由新进程使用；复用已有功能测试，本次未调用真实模型|
|迁移与数据|仍为 `f2d91a4c8b77`；控制端前后只读快照一致；历史 Session/Run/Artifact 可读，未恢复、删除或清理历史任务|

## 1. 指定 PTY 关闭与资源保护

关闭前重新核对，而非使用旧 PID 直接终止：

- PTY `4be4256d-ba39-4738-a2d8-60e18a77bae3`，Run `0442502b-62da-4b35-9341-1220af87914f`，Session `ac9dabc2-ccee-48c2-bb7d-534e8a6a3bac`，台账 running/available。
- PID 3328015；台账与 `/proc` 启动 ticks 均 36836333；可执行文件 `/usr/bin/nc.traditional`，进程组身份一致，组内仅该目标进程。
- 控制端没有 queued/running/waiting_tool Run、有效运行租约或 reserved/running Worker 请求；没有其他活动 Executor Job/PTY。没有新任务接管该终端的证据。

使用已有 `/execution/v1/operations` 的 `terminal_close`，携带原 Session/Run 当前 fence；没有修改或接管旧任务的 fence。关闭操作 ID：`9b2b07cd-be50-428d-8f3d-f56b33b5475b`。

正常接口返回 succeeded、process_state=closed、process_confirmed_exited=true、termination_reason=user_cancelled。随后独立核对 `/proc/3328015` 不存在、原进程组无存活成员、Executor active_jobs=0。没有使用 pkill/killall、SSH 手工 kill 或关闭其他终端。

控制端前后数据库快照完全一致：completed 72 / failed 5 / cancelled 6 / recovering 5 / paused 8；Session、Worker 请求与 18 条 visible_events 状态保持。旧 nc 所属 Run 仍保留 recovering，不伪造 completed。历史 unknown Job 22 条保留。

生产普通 Job 的 succeeded 从 2534 增至 2536，正好是本次两个无害验收 Job；failed 105、cancelled 7、unknown 22 不变。旧 PTY 记录、工作区、工件、Session 和日志均留存，没有删除。

## 2. Kali 完整部署与命令注册

仅为应用部署构建一次当前 Executor wheel，未重建前端/Worker。使用现有 `scripts/deploy_executor.py`，先确认资源空闲，上传完整发布，再由原 executor-control 停止旧服务、安装应用 wheel、同步完整源码并启动新实例。没有在运行中逐个覆盖模块。

- 发布目录：`/home/kali/rexak-agent-executor/releases/20261108T071919170060Z`。
- wheel SHA256：`69ce857681f5ac119637c4ed5749e1b2c771ef3735f52a605206fde9eb00484a`。
- 生产实际模块：`/home/kali/rexak-agent-executor/.venv/lib/python3.13/site-packages/workbench_executor`。
- 新实例启动：北京时间 15:20:13；旧 boot `3135933-681aa2e84113` 已替换为 `3485002-2470199d96ea`。
- 27 个 Python 文件逐项一致，无差异；新增 builtin_commands.py 已安装。部署入口同时核对了身份、协议、trusted 配置及依赖完整性。
- 原 venv 未重建；仅替换 Executor 应用包，已有依赖均满足，没有安装、升级 Pwn/Web 工具。

随后只执行一次现有维护入口：

```bash
.venv/bin/python -m workbench_executor.builtin_commands \
  --data-root /home/kali/rexak-agent-data \
  --toolchain-root /home/kali/rexak-agent-data/toolchains/pwn-20261108
```

安装器检查全部目标可执行文件、GEF 脚本及同名冲突后，生成六个普通命令入口；没有注册 python/python3/pip 或 Web 命令别名。没有修改系统 Python、系统 PATH、全局 profile，也没有 source activate.sh。生产 Job 实际 PATH 的最后一项已经是 `/home/kali/rexak-agent-data/tools/bin`，证明加载的是新版环境逻辑。

授权配置及执行规则保留；network_isolation 仍如实报告 unavailable，没有更改 iptables、Docker 网络或放宽授权。真实调试、进程组和超时实现沿用原代码，本次不重跑已通过的超时/PTY 功能测试。

## 3. Windows 一次必要更新

停止前备份原 launcher API/Runner/Worker 日志，目录为 `work/release-log-backup/20261108T071625Z`。最初针对 API 子进程组的 Ctrl+Break 未触发退出；随后核对原控制台仅包含这套项目进程和维护助手，以该专属控制台 Ctrl+Break 进入项目既有 SIGBREAK/finally 清理。所有旧项目进程均退出，没有强杀或影响其他控制台。实际服务停止、启动各一次。

原入口以隐藏窗口启动一次：

`C:\xjs\rexak-agent\start-workbench.cmd --no-browser --no-recover`

该参数使旧 recovering/paused Run 不被自动恢复，新 Runner 仍正常接收后续新任务。原入口完成数据库/SSH/Executor 检查、API/Worker 就绪与 Runner 启动。已有迁移版本正确，没有实施新迁移。没有重复构建未过期的 web/dist、worker/dist，也没有切换模型配置。

新进程都来自本仓库原入口，启动时间晚于附件修复、历史持久化和 Main 进度查询修改时间。当前模块定位仍在项目 src；get_delegated_task 的现有工具元数据包含 delegation_id、history_cursor、history_limit。对于该工具的延迟导入，旧进程已完全退出，后续导入使用当前源码；本次没有用模型实际调用它，也不将健康接口的局部内存指纹冒充此函数的独立内存哈希。

API 当前启动指纹与同范围源码一致；前端静态文件匹配磁盘，源码没有晚于构建的项。Pi Worker 运行 bailian/deepseek-v4.1-flash，health protocol=1.0，模型参数与部署前一致；P0 相关 JS 构建均早于新 Node 启动，visible_events 存储逻辑及严格附件协议使用现有最新版。

迁移仍为 f2d91a4c8b77，真实 Worker 角色可读 visible_events 且有 INSERT 权限，18 条事件/4 个请求保持。真实写入、失败/取消/中断、report_finding 与进度分页的测试均复用此前成功证据，没有重复写测试数据。

## 4. 本次仅两笔生产命令验收

通过真实生产 Executor 的认证 HTTP 接口、既有 fence 和命令信封执行，无 Main/Pi 模型调用。使用独立维护 Session/Run 标识，只在 Executor 新增两笔完成记录，未向控制端创建 CTF Run、Session 或 Worker 请求。

|生产 Job|内容与结果|
|---|---|
|Main `60c26e01-fc0f-46be-b78f-094149b08615`|同一 bash 执行 Nmap 7.99、Nuclei v3.8.0、Sqlmap 1.10.6#stable；默认 Python 路径/来源；六个命令查找、GDB/checksec/ROPgadget/patchelf 启动、GEF 加载与版本、Pwn 导入；退出 0，PWN_OK|
|Worker `972a5744-30fa-4248-83c7-2c8c81d2d606`|同一组普通命令入口可达；默认 Python 未变，私有 cwd/HOME/TMPDIR 核对及 Pwn 导入；退出 0，WORKER_ENV_OK / WORKER_PWN_OK|

两笔完成回执均为 group_confirmed_exited=true，最终 active_jobs=0。全部命令没有 source/activate，没有扫描真实目标。

实际默认 Python 为 `/usr/bin/python3`、3.13.12、prefix=/usr；requests/urllib3 仍来自 `/usr/lib/python3/dist-packages`，没有继承 VIRTUAL_ENV/PYTHONHOME/PYTHONPATH/LD_LIBRARY_PATH。Main HOME 仍为 `/home/kali`。

Worker cwd 为本次维护 Session 下 `.rexak/workers/9854038d-62ef-450e-b526-75e356de8c1c`；HOME 为其 `.home`，TMPDIR 为其 `.tmp`。两者 PATH 的既有顺序相同，只在末尾增加生产 tools/bin。

历史 Session 的 snapshot/conversation/delegations 均 HTTP 200；另对一个已有可用 Artifact 执行一次 limit=1 的有界预览，HTTP 200，未输出正文或凭据。

## 5. 证据与测试范围

本次新执行的部署确认仅包括：指定终端关闭与身份复核、必要 Executor wheel 构建/部署、六命令注册、一次 Windows 受控重启、两笔生产命令、服务/模块哈希/数据库/静态资源核对，以及历史 Artifact 一次有界读取。

没有重跑此前 14 项附件集成、进度/协作 Mock、17 项 Executor 定向测试、8 项 Pi 适配测试、独立 ELF/GDB/PTY 验收或真实 PostgreSQL Mock 写入。它们继续引用 P0、进度查询、Pwn 工具链、统一环境和 finding 审计报告中的成功证据。本次应用源码没有因部署修改。

主要证据：

- `work/release-terminal-close.json`：关闭前身份、原 fence、正常关闭回执、PID/进程组退出及台账核对。
- `work/release-kali-deploy.json`、`.log`：完整发布、27 文件哈希、新 boot、身份与协议。
- `work/release-registered-tools.json`：六个入口的实际内容及 SHA256。
- `work/release-windows-stop-final.json`、`release-windows-after.json`、`release-listeners-after.json`：旧进程退出、新进程及监听关系。
- `work/release-launcher.log`：原入口冷启动，Runner 未自动恢复历史任务。
- `work/release-production-smoke.json`：两笔真实生产 Job 的完整信封、输出、资源状态及完成回执。
- `work/release-preflight-evidence.json`、`release-final-evidence.json`、`release-final-summary.json`：部署前后只读快照、实际包路径与哈希、历史数据不变核对。
- `work/release-artifact-check.json`：历史工件有界读取结果。
- `work/release-report-before.md`：更新前受阻报告备份；原服务日志也已备份保留。

## 6. 放行结论

**READY。** 必要生产部署与最小命令验收已完成，没有遗留活动 Job/PTY 或代码同步阻断；5 个 recovering、8 个 paused Run 及历史 unknown Job 按要求保留，不影响为下一轮创建新任务。

完整真实 A2A ZIP→Pi→Executor 联合行为、模型主动 finding/进度消费、真实 CTF 解题效果留待用户下一轮授权真实测试观察。这些不再作为重复工程验收的理由。本轮没有调用付费模型、运行 CTF 或 Git commit/push，至此停止部署验收。

<details>
<summary>历史：此前受活动 PTY 阻断的只读核对（已由上面的生产部署结果取代）</summary>

# 真实测试前最终部署核对

核对日期：2026-11-08，北京时间约 14:52–14:56。**结论：BLOCKED，不满足本轮要求的“全部新版本、统一工具环境已在生产生效”条件。**

本次重新查询到原生产 PTY 仍有真实进程，按要求保留全部相关服务，没有停止、取消、接管或重启；没有将候选文件写入生产安装目录，没有注册生产工具。已完成其他安全可执行的版本、桌面入口、服务、静态资源和数据库核对。本次未运行 pytest、前端 E2E、隔离 Executor 验收、构建、工具安装、真实 CTF、扫描或模型请求，没有 commit/push。

## 1. 最终结果

|检查项|本次结果|判断|
|---|---|---|
|桌面快捷方式与入口|`Rexak Agent.lnk` 指向 `C:\xjs\rexak-agent\start-workbench.cmd`，工作目录正确；入口以自身目录启动项目 `.venv`|通过|
|实际入口执行|执行一次 `start-workbench.cmd --no-browser`，退出 0，返回“已打开现有 Rexak Agent 控制台”；走现有 API/Worker 复用分支|通过；未验证新服务冷启动或浏览器视觉渲染|
|API|127.0.0.1:8770，PID 36596，健康及数据库访问正常；启动和当前的有界内存指纹一致，且与当前源码同范围指纹一致|基础服务通过；不是整个进程的版本证明|
|Runner|PID 7416，由当前项目原 `workbench_child.py runner` 启动；进程存在，日志持续更新|运行入口正确；最新进度查询加载未确认|
|Pi Worker|PID 39668，127.0.0.1:8795，当前项目 `worker/dist/server.js`；health 与 A2A Card 可访问；构建未过期|部署时序与 P0 历史修复一致；没有真实模型联合验收|
|Kali 安装源码|从实际 `.venv` 解释器导入的 site-packages 包逐项计算 SHA256；本机 27 文件，生产 26 文件，存在 8 项差异|未同步，阻断|
|Kali 运行版本|原 PID 3135933、原 boot ID，生产安装包仍为原基线|尚未加载新版，阻断|
|六个生产内置入口|`/home/kali/rexak-agent-data/tools/bin` 下六项均不存在|未注册，阻断|
|Web / 默认 Python|复用此前同环境兼容性成功证据；本次未启动旧生产 Executor 的工具 Job|未做新版生产验证，不能标生产通过|
|迁移、历史表|当前 `f2d91a4c8b77`；visible_events 存在，Worker 角色可读且有 INSERT 权限，读到 18 条/4 个请求|数据库条件就绪；未重复迁移或写入测试数据|
|Main 新进度查询|相关 Python 源码修改于 13:27，API/Runner 启动于 12:56；健康指纹不覆盖该工具|运行中绑定未确认，需安全加载后验收|
|现有数据访问|Session 列表、历史 Session snapshot/conversation/delegations 均 HTTP 200；服务提供当前构建的 Ultra 标签资源|只读访问通过|

## 2. 生产活动资源：本次实际查询

本次通过 SSH 以 SQLite `mode=ro` 查询 Executor 台账，并核对 Linux `/proc`，不是沿用旧报告判断：

- PTY：`4be4256d-ba39-4738-a2d8-60e18a77bae3`。
- 台账：`state=running`、`interaction_state=available`。
- PID：`3328015`；台账 start ticks 和实际 `/proc` start ticks 都是 `36836333`，进程身份一致。
- 实际可执行文件：`/usr/bin/nc.traditional`；进程状态 `S` 表示睡眠等待，不能解释为已退出或可以删除。
- cwd：`/home/kali/rexak-agent-data/workspaces/ac9dabc2-ccee-48c2-bb7d-534e8a6a3bac`。
- 所属 Run：`0442502b-62da-4b35-9341-1220af87914f`，Session：`ac9dabc2-ccee-48c2-bb7d-534e8a6a3bac`。

普通活动 Job 为 0，但活动 PTY 为 1，所以 `/health active_jobs=1` 合理。普通 Job 计数为 succeeded 2534 / failed 105 / cancelled 7 / unknown 22。没有向该 PTY 发输入、连接其目标、关闭 nc、修正台账或清理历史 unknown。

控制端数据库本次快照：Run 为 completed 72 / failed 5 / cancelled 6 / recovering 5 / paused 8，没有 running/waiting_tool/queued Run；5 个 recovering Run 的租约已过期，但其中一个仍对应上述实际执行进程，不能因租约过期就判定可安全重启。Worker requests 没有 reserved/running，保留 finished 18 / cancelled 1 / interrupted 1。

Session 元数据为 active standard 51、active ultra 40、deleted standard 2；Session 的 active 不等于正在执行模型任务。未恢复的 5 个 Run 和暂停的 8 个 Run 均保留。

## 3. Windows 桌面与服务版本

### 入口、端口和进程

`start-workbench.cmd` 使用 `cd /d "%~dp0"`，再调用项目 `.venv\Scripts\python.exe scripts\start_workbench.py`，没有指向 `C:\xjs\xak-agent` 或其他旧路径。

|组件|实际进程 / 启动时间（北京时间）|启动路径|
|---|---|---|
|原启动器|实际 Python PID 3876，12:56:16|`scripts\start_workbench.py --no-browser`，项目 venv 启动链|
|API|PID 36596，12:56:36|`C:\xjs\rexak-agent\scripts\workbench_child.py api`|
|Runner|PID 7416，12:56:46|同一 `workbench_child.py runner`|
|Pi Worker|PID 39668，12:56:45|`C:\Program Files\nodejs\node.EXE C:\xjs\rexak-agent\worker\dist\server.js`|

Windows venv 的 python.exe 引导进程及其实际 CPython 子进程属于同一启动链，不当作重复 API/Runner。监听者只有 API PID 36596 的 8770 和 Worker PID 39668 的 8795，均为 127.0.0.1。入口本次仅复用服务，没有进入数据库初始化、迁移、Executor 部署或 Runner 恢复分支。

API `/health`、`/console/` 均 200，数据库 ok。API 内存指纹在启动时、当前健康请求及本机同范围函数计算中均为：

`loaded-runtime-v2:5d73ebd5d1d6f6c3807c1dec26e9b5318e15c06e`

其 scope 明确是 `loaded_entrypoints_and_history_assistance_v2_not_entire_process`。这不包含新增 `get_delegated_task` 进度查询，不能据此确认该函数已经加载。

API/Runner 均在 12:56 启动，但 `tools/delegation.py` 和新增 `domain/worker_progress.py` 修改于 13:27。委派工具存在延迟导入路径，不能仅凭进程启动早就精确断言其当前所有绑定；同样也不能因文件在磁盘就判定运行时是新版。本次保留为**最新进度代码加载未确认**，没有注入进程、热重载或用模型触发导入。

附件契约及委派管理修复文件修改于 12:41、12:44，早于当前服务启动，结合此前 P0 部署记录属于已部署时序；同笔真实 A2A 联合行为仍未通过本次验收。API/Runner 日志持续更新，日志中未见 Python traceback；这仅证明基础运行状况，不替代新功能验收。

### Worker 构建与数据库访问

Worker P0 相关 history/pi-runtime/executor/store 构建文件均在 12:55:46 生成，晚于对应源码并早于 Node 启动；本次不存在 `worker/src/*.ts` 新于相应 dist/*.js 的项，未重复构建。相关源码和构建产物 SHA256 已记录在证据文件中。

真实 Worker health：`runtime=pi`、`provider=bailian`、`model=deepseek-v4.1-flash`、`protocol=1.0`。A2A Card GET 返回 200，包含 `evidence-delegation` skill。没有更改其模型配置或提交可能产生费用的 A2A 任务。

数据库 `app.alembic_version=f2d91a4c8b77`，已满足 visible_events 迁移。本次以业务连接及真实 Worker 独立角色的**只读事务**读取表；独立角色的 SELECT/INSERT 权限均为 true，表内保留此前已验证的 18 条事件、4 个请求。真实写入与按 Session 读回复用此前四种 Mock 任务的成功证据，本次不重复写数据。

### 静态页面与历史访问

API 返回的 index.html 与 `web/dist/index.html` 完全一致；返回的当前资源与磁盘逐字节一致：

- `index-CBbDKEm2.js`：SHA256 `d4090e3499e8530b5b7e7081147c687bf6f2cdb4c493dd62289f48fb64a247c5`。
- `index-Cxqjvw_j.css`：SHA256 `0e66ba9009a3ca2a55bac27f51ba55bcf671466edd1b4f1dbc2913e514b209c5`。

web/src 没有比当前构建更新的文件；当前已服务的 JS 包含 Ultra 标签。本次未重复构建或执行完整浏览器 E2E，不能写成视觉交互全通过。

通过现有本地鉴权只读访问 Session 列表，以及历史 Session `e71a7c75-9e1c-4f6b-8639-a9ccc64be13f` 的 snapshot、conversation、delegations，四项均 200。没有打印历史正文、Cookie 或访问令牌。

## 4. Kali 同步与内置命令状态

生产实际导入路径为：

`/home/kali/rexak-agent-executor/.venv/lib/python3.13/site-packages/workbench_executor`

本次不是只比较仓库镜像目录：使用生产 venv 解释器导入定位该包，对其全部 Python 文件逐项计算 SHA256，再与 Windows `executor/src/workbench_executor` 比较。

|项目|实际状态|
|---|---|
|本机源码|27 个 Python 文件|
|生产实际安装包|26 个 Python 文件；逐项哈希与之前保留的旧生产基线一致|
|有差异/缺失项|`api.py`、`command_runtime.py`、`execution_environment.py`、`job_worker.py`、`processes.py`、`settings.py`、`terminals.py`；缺少新增 `builtin_commands.py`|
|服务|executor_id `kali-01`，版本 0.1.0，协议 1，解释器为生产 venv|
|实际服务进程|PID 3135933，UID 1000，cwd `/home/kali/rexak-agent-executor`；底层 Python 3.13|
|boot ID|仍是 `3135933-681aa2e84113`，启动于 2026-11-07 11:26:28 北京时间|
|网络能力|仍报告 `network_isolation=unavailable`，未更改原 trusted 决策或授权规则|
|六个入口|`tools/bin/gdb / gef / checksec / ROPgadget / patchelf / pwn-python` 均不存在，也没有同名符号链接|

运行服务仍沿用旧部署，不能声称已加载统一 PATH 追加逻辑。由于活动 nc PTY 仍在，没有覆写安装目录或仓库镜像，避免后续子进程混用新旧代码。独立 Pwn 工具链保留原样，没有重新下载、安装或激活它。

新版生产加载是执行 Web/Pwn 生产冒烟的前提，当前前提不成立，因此本次**没有**向旧生产 Executor 提交工具验收 Job，没有借 SSH 版本输出替代真实生产 Main/Worker 验收。

## 5. 复用的历史证据：本次未重跑

|能力|已有成功证据与当前边界|
|---|---|
|严格 A2A 附件、Worker 路径交接|`p0-engineering-fixes.md`：14 项控制端/附件定向集成通过，Worker 严格字段和本地路径分段通过；完整生产同笔 ZIP→A2A→Pi 读取仍未验证|
|visible_events 持久化|P0 报告：真实 Worker 角色 + Mock Pi 的 completed/failed/cancelled/interrupted 四种任务，分别读回 6/2/4/6 条事件；本次只核对表、权限及保留记录|
|Main 研究进度/分页|`worker-progress-query-implementation.md`：2 项单元及 5 项协作集成通过；本次没有重跑，运行中的新增函数加载尚未确认|
|report_finding|`report-finding-audit.md` 与进度报告所引 5 项协作 Mock 证据；相关 collaboration 源码仍为 11 月 5 日文件，本次未修改、未重复测试；不等同真实模型已主动上报|
|Pwn 工具安装、ELF/GDB/PTY/pwntools|`pwn-toolchain-setup.md`：独立工具链实际执行通过；本次不重复安装或验收|
|统一环境与 Web/Python 共存|`ultra-unified-toolchain.md`：同机候选 Executor Main/Worker Web→Pwn 连续执行、默认 Python 不变、PTY/超时通过；17 项 Executor、2 项控制端及 8 项 Pi Mock 通过；始终限定隔离实例|
|失败、取消、unknown|沿用上述既有定向测试；本次不改状态机，不重放旧操作，不把历史 unknown 当成可重试失败|

## 6. 本次真正执行的验收与成本

1. 一次 Windows 进程与监听快照、桌面快捷方式和启动脚本核对，记录启动路径、父子关系和时间。
2. 一次只读部署探针：API/Worker/Card、当前页面及 JS/CSS 哈希、四个历史只读接口、数据库版本/状态/权限；SSH 读取实际安装包哈希、活动台账及 `/proc` 身份。
3. 一次本机同范围 API 指纹计算与文件时间核对，复用已有生产旧包哈希基线；读取服务日志的大小、修改时间与 traceback 计数，不触发任务。
4. 一次真实原入口 `start-workbench.cmd --no-browser`，成功复用现有服务；入口内部正常健康检查不构成新模型或执行任务。
5. 本报告及维护探针的 UTF-8 无 BOM 检查；没有运行工程回归测试。

本次没有构建、同步生产文件、注册工具、数据库迁移、生产 Job/A2A 任务提交、工具版本执行、隔离实例重验、pytest 或 npm 测试。未创建新 Session/Run/Artifact 或 Worker 请求。

证据文件：

- `work/final-deployment-evidence.json`、`work/final-deployment-probe.log`：本次全部只读服务、数据库及安装哈希快照。
- `work/final-windows-processes.json`、`work/final-windows-listeners.json`：Windows 启动与端口关系。
- `work/final-local-version-summary.log`、`work/final-static-followup.log`：指纹、时序、构建及日志核对。
- `work/final-desktop-entry.log`：原入口本次执行结果。
- `work/final_deployment_probe.py`：本次只读探针，不包含模型调用、部署或任务取消动作。

## 7. 放行条件

当前不是仅剩可选验证的 PARTIALLY READY：统一工具环境**尚未部署到生产**，Main 最新进度功能也**未确认已加载**，因此结论为 **BLOCKED**。

需要先让原真实执行资源经其所属任务的正常流程安全结束；本次不擅自处理。之后仍需重新确认没有活动 Job/PTY，再通过原流程完成：

1. Windows API/Runner 的必要安全加载，确认新增 get_delegated_task 绑定；未变化的 Worker 构建不必重复重建。
2. 完整同步 Executor 新版、注册现有六个普通命令、受控启动新实例，核对新 boot ID、实际模块和逐项哈希；保留历史数据。
3. **仅在该新版生产实例上各执行一次**最小 Web/Pwn 无害命令及 Main/Worker 环境检查，无需重跑已有隔离验收。

完整真实 Pi 联合链路仍由后续不调用付费模型的合适条件或下一轮用户授权真实测试验证；不能为了完成验收切换当前付费模型配置。基础服务、页面和数据访问目前可用，但本报告不放行下一轮所要求的完整新版 CTF 测试。

</details>

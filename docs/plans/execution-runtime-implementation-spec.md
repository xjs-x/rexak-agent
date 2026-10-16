# 命令执行底座完整编码规格

版本：1.0，2026-10-15。状态：用户已确认总体方案，本文件是交给编码智能体的独立实现规格。

本文件完整描述要求、默认行为、协议、模块划分、实现顺序及交付标准。编码不依赖任何外部参考项目、源码目录或此前对话。可以阅读本工作区现有文件以定位集成点；不能以缺少外部项目为理由遗漏功能。伴随文件 `execution-runtime-acceptance.md` 定义验收，`execution-runtime-handoff-prompt.md` 是任务提示词。

## 0. 工作目录、责任与一次提交

- 唯一仓库：`C:\xjs\rexak-agent`，不能修改 `C:\xjs\xak-agent`。
- 编写本规格时分支为 `codex/implementation-v1`，HEAD 为 `a3d9df06fa804374fc24c77c5f2140fe7e79146c`。开工必须重新核对实际状态；不得 reset 到这个历史值。
- 当前已有未提交改动：`src/agent_workbench/api/routes.py`、`tests/integration/test_web_model_settings.py`、`web/src/api/client.ts`、`.workbuddy/**`，以及未跟踪实验资料。先记录实际基线，再增量编辑；不覆盖、回滚或混入已有改动。
- 编码智能体负责完整实现、迁移脚本、隔离自测、验收脚本及自测报告。验收智能体负责独立复核与真实部署验收；编码智能体不得把自测报告命名为最终验收通过。
- **只做一次最终 Git 提交。** 中间不提交阶段性 commit，不提交空实现。全部实现及可运行自测完成后，逐项暂存本次改动，检查暂存差异，统一提交；不 push、不创建 PR。
- 仅提交本次新增/改动的代码、协议生成物、锁文件、测试和相关文档。已有用户改动必须保持其原有暂存/未暂存状态；重叠文件按 hunk 区分，不使用 `git add .`。
- 若本次功能确实依赖既有未提交改动，先消除这种隐式依赖；不能私自吞入原改动。无法分开时如实报告具体依赖，不伪造独立可复现的提交。
- 新增的三份编码/验收/交接文档及本次设计文档属于本次交付，可随最终 commit 纳入。`.workbuddy` 和既有实验目录不在范围内。
- 只在确有影响实现的歧义时汇报；不得反复询问已经确定的产品选择。依赖版本、模块内部分拆和资源默认值按本规格及实际环境验证解决。

## 1. 不可变约束

1. 唯一模型—工具循环继续使用官方 `create_agent`。只能扩展工具、middleware、Runner 调度及领域服务，不创建第二套 Agent 循环。
2. checkpoint 保存图状态；控制端业务表保存操作、委托及领域记录；Executor 台账保存真实执行状态；工件保存正文。控制端的进程状态只能是只读投影。
3. 工具成功、进程退出、证据完整、发现 confirmed 是不同事情。命令退出码零不能直接确认漏洞或完成任务。
4. 操作身份先持久化再派发。重放复用 `operation_id`；显式重做使用新逻辑操作；工具函数不得临时随机生成操作 ID。
5. 不设任务累计时长或累计费用硬上限。执行超时、单次等待、输出和并发资源边界仍存在。
6. UTF-8 无 BOM；中文可读；未知输出字节保留能够采集的原件并明确编码错误，不静默尝试 GBK/Latin-1。
7. 取消、超时、参数错误、协议错误、网络不确定分别处理。不得吞异常后返回成功或空对象。
8. Executor 及任务进程不接收模型 API Key 或控制端数据库凭据；管理令牌不进入命令子进程环境。
9. 运行文件位于 `WORKBENCH_DATA_ROOT` 或 Executor 对应数据根，不写源码包。
10. 未实现、未验证和不可用能力如实展示。真实目标、真实模型效果实验由用户执行，本次不执行。

本规格是用户已确认的新产品决策，明确替代旧版“缺少出站隔离即完全关闭命令”的默认策略。实施时新增正式 decision 并更新状态文档；不能把旧关闭文案解释为仍需用户重复批准可信执行。

## 2. 已确定的用户行为

| 编号 | 必须实现的行为 |
|---|---|
| R01 | 指定 Kali 采用 `trusted` 执行模式，普通命令不逐条审批；`network_isolation=unavailable` 可以同时成立 |
| R02 | 默认独立命令；需要连续输入、目录/环境延续时打开持久终端；共享同一对话工作区 |
| R03 | 默认交互等待 10 秒，可配置；到期返回原 job 和已有输出，不重复启动；执行截止时间独立 |
| R04 | 非交互长任务在 Executor 服务正常重启和崩溃期间继续运行，配额内日志持续保存；恢复后可查、可读、可取消 |
| R05 | 交互 PTY 允许在 Executor 重启后失去交互能力；保留记录、核对残留进程，不自动重建或重发输入；不新增常驻终端接管服务 |
| R06 | 按具体任务/事件等待，默认任一匹配结果即可继续；明确要求汇总才等全部；长期资源不阻塞其他工作 |
| R07 | 阶段结束保留资源供续聊；必须可见、可终止、有配额；无输出只提示，不按空闲时间自动清理 |
| R08 | 采集日志超限默认保活、持续排空并明确标注缺失；只有显式设置才因超限终止；不能按程序名、前后台、PTY 或一次性任务推断 |
| R09 | 资源存活与值班委托独立；阶段结束默认存活但未委托；取消委托不关闭资源；持续处理需明确委托 |
| R10 | 无委托/暂停期间事件持续记录展示；下次对话以聚合摘要注入；UI 已读与模型已获知分开 |
| R11 | 基础环境部署时准备；任务依赖在对话虚拟环境/本地依赖目录内安装复用；系统软件走环境维护流程 |
| R12 | 兼容现有 HTTP、浏览器、文件、证据、暂停续聊、模型配置、弱点与线索系统；不改写其业务含义 |

“默认保活”仅指不因采集日志超限主动杀进程，不承诺进程在磁盘故障、机器重启或自身异常后继续正常运行。已有输出配额与程序自行写文件的磁盘边界分别处理。

## 3. 当前代码入口与需要解决的问题

以下定位以当前工作区为准，实施前按符号重新核对。

| 位置 | 当前情况 | 本次工作 |
|---|---|---|
| `tools/registry.py` | 无命令组；关闭原因是缺少出站隔离 | 新工具组及能力发现，可信模式条件启用 |
| `tools/operation.py`、`tools/remote.py` | 已有稳定操作身份和远端受理骨架 | 复用；区分执行操作结束与资源仍存活 |
| `tools/jobs.py` | job 主要按当前 run 解析 | 同 session 跨 run 读取/管理，保留原派发方向标识 |
| `execution/client.py` | 已有 HTTP Executor 客户端 | 增加新执行协议、事件补读、会话资源和 fencing 同步 |
| `runtime/jobs.py` | 所有未决 job 结束才唤醒 | 精确 wait-set、事件订阅、委托和事件聚合 |
| `runtime/runner.py`、`runtime/recovery.py` | 已有调度、租约及恢复 | 接入新资源，不能复活旧 tool call 或第二次执行 |
| `agent/middleware/tool_dispatch.py` | 控制版本和归属检查 | 检查新工具、委托版本，保留合法工具消息闭合 |
| `agent/middleware/finish_gate.py`、`tools/completion.py` | 阶段完成判定 | 区分待办、资源和持续委托，禁止资源伪装成已完成工作 |
| `executor/.../policy.py` | `generic_command_allowed=False` | 按可信配置放行新执行入口，保留诚实的网络隔离报告 |
| `executor/.../processes.py` | Executor 持有 subprocess 句柄，文件输出，尺寸标记 | 非交互独立包装进程、限额采集、恢复后取消 |
| `executor/.../api.py` | 普通 command API 关闭，事件列表主要在内存 | 新执行路由；持久事件；旧 HTTP/浏览器接口兼容 |
| `executor/.../ledger.py` | SQLite 接收、job 与状态 | 有版本迁移，唯一 operation_id，资源/输入回执/事件/控制 fence |
| `executor/.../filesystem.py` | workspace 文件读写和版本校验 | 同一工作区、批量编辑和检索 |
| `deploy/executor_control.py`、service 模板 | 管理服务进程 | 服务重启与任务终止分离，不能误杀存活任务 |
| `scripts/deploy_executor.py` | 上传源码、wheel、核对租约与版本 | 打包新模块/生成协议；资源预检、兼容升级及证据输出 |
| `web/src/ToolActivity.tsx`、`App.tsx`、`api/client.ts` | 工具活动和控制台 | 资源、值班、等待、输出缺失和取消确认展示 |

当前 `jobs` 表唯一索引为 `(operation_id, request_digest)`，不足以在并发下禁止同 ID 不同参数；必须改为 operation_id 本身唯一并实现事务化冲突处理。当前 `remote_events` 内存列表和进程内计数不能承担跨重启补读，必须改造。

## 4. 模块边界与新增文件建议

以职责完整和依赖方向为准，允许等价分拆，但不得遗漏。

```text
src/agent_workbench/contracts/execution_runtime.py     新协议模型、参数和新增枚举
src/agent_workbench/tools/commands.py                  命令、脚本、监视工具
src/agent_workbench/tools/terminals.py                 PTY 工具
src/agent_workbench/tools/execution_resources.py       等待、列表、值班和环境工具
src/agent_workbench/domain/execution_delegations.py    委托授权、撤销及触发服务
src/agent_workbench/runtime/execution_events.py        事件拉取、去重、聚合交付
src/agent_workbench/api/execution.py                   资源与委托 API，挂入已有 app
executor/src/workbench_executor/execution_api.py       新执行路由
executor/src/workbench_executor/command_runtime.py     任务创建、恢复与终止
executor/src/workbench_executor/job_worker.py          非交互任务包装进程
executor/src/workbench_executor/terminals.py           PTY 生命周期与输入
executor/src/workbench_executor/output_store.py        有界采集、游标、缺失区间
executor/src/workbench_executor/execution_events.py    持久执行事件
executor/src/workbench_executor/execution_environment.py 环境与工具发现
executor/src/workbench_executor/_generated/contracts/  自动生成协议副本
scripts/generate_executor_contracts.py                 协议生成及 --check
scripts/accept_execution_runtime.py                    隔离 Linux 进程演练
scripts/accept_execution_runtime_ui.py                 确定性界面演练
```

普通进程和 PTY 共用参数校验、环境构建、操作身份、结果、输出策略及取消组件。两种传输机制分开实现，不能为了复用强行让所有命令通过 PTY。

## 5. 协议真源与兼容策略

### 5.1 生成链

- 新协议真源在 `src/agent_workbench/contracts/`，纯 Pydantic 模型只依赖标准库及 Pydantic，不加载设置、模型 SDK 或数据库。
- 推荐确定性源码生成方式：将纯协议模块及其依赖闭包复制到 Executor `_generated/contracts/`，仅改写包内导入前缀，保留模型逻辑、约束和枚举；生成头标注源哈希，禁止手改。
- `generate_executor_contracts.py --check` 比较生成结果并对漂移失败；禁止在生成过程中偷偷改行为。执行端 wheel 必须含生成物。
- 控制端 OpenAPI 由实际 API 模型导出，前端通过现有 `generate:api` 生成，不手写第二套状态枚举。
- 新命令正文、stdin、终端 text/paste 等字段禁止 `str_strip_whitespace=True`，必须保留首尾空格、换行及控制字符；路径/名称单独校验。

### 5.2 兼容协商

采用增量执行子协议：`/execution/v1/...`；原 `/jobs` 等 HTTP/浏览器接口保留。现有全局 protocol version 不应仅因新增能力就强制破坏旧接口。

`/capabilities` 增加版本化 details 或明确字段：`execution_runtime_versions`、`execution_mode`、`command`、`script`、`pty`、`screen`、`monitor`、`durable_events`、`detached_job_recovery`、`output_limit_actions`、`workspace_storage_enforcement`、实际依赖及不可用原因。旧执行端缺少执行子协议时，新工具确定不可用；不得回退到控制端执行命令。

可信配置使用显式 `EXECUTOR_EXECUTION_MODE=disabled|trusted`，发行缺省保持 disabled，个人部署显式设 trusted。命令可用还需解释器/平台/初始化预检成功，不能只看配置文字。HTTP/浏览器范围校验不变。

### 5.3 身份、控制与操作包

新增公共操作包至少包含：

```text
schema_version, operation_id, session_id, run_id,
tool_call_id, request_digest, action, arguments,
owner_generation, dispatch_control_version, session_control_seq,
delegation_id?, delegation_revision?, created_at
```

其中身份、版本、工作区和摘要由控制端可信代码补齐，不由模型自由指定。`operation_id` 沿用 `OperationRegistry`；request_digest 基于规范化后且影响执行的参数，执行端独立校验。重试不创建新资源。

- Executor 持久保存按 session 的执行 fence，包含当前 run/owner/control 及有序的 `session_control_seq`。顺序可复用控制变更事件分配的 session_seq，不新增第二份用户目标真源。
- 受信控制同步更新 fence，工具请求只能匹配当前 fence；旧 run、旧 owner、旧方向及已撤销委托的操作不得靠携带更大的数字自动获得执行权。
- 控制端仍在派发前检查当前数据库状态，Executor 再检查已收到的 fence。暂停/取消的远端同步有明确 pending/acknowledged 状态，不承诺尚未送达的撤销能阻止所有在途操作。
- 所有终端读写与资源操作都核对 session 归属。跨 run 续用允许，跨 session 不允许。

### 5.4 公共返回值

继续使用 `ToolResult`，增加或通过 typed data 提供：

```text
operation_status, execution_certainty,
resource_id?, job_id?, terminal_id?, process_state,
exit_code?, termination_reason?, wait_reason?,
output_pages[], artifact_refs[], capabilities_note?
```

工具结果成功表示本次查询/打开/发送操作成功，不自动表示被控制程序结束。`wait_reason` 至少覆盖 `output/prompt/quiet/exited/wait_timeout/interrupted/watch_match`。

新增资源状态建议为 `starting/running/exited/closed/unknown`，另有 `interaction_state=available/lost/not_applicable`；已有 JobState 继续用于 job 投影。不要把交互失效等同于进程已退出。退出原因区分正常退出、执行超时、用户取消、显式输出超限终止和故障。

错误码至少覆盖：`execution_disabled`、`dependency_unavailable`、`operation_conflict`、`stale_control`、`resource_not_found`、`resource_owner_mismatch`、`interaction_lost`、`input_sequence_conflict`、`execution_timeout`、`resource_limit`、`output_gap`、`invalid_encoding`、`uncertain_after_restart`。错误字段稳定，不从中文文本猜测状态。

## 6. 工具和服务行为规格

下面列出的参数是模型可见入口的主要字段；内部统一补 operation/session/run/fence。副作用工具取得 `runtime.tool_call_id` 后走既有持久操作骨架。工具名可做兼容别名，但主要入口不得重复注入模型。

### 6.1 命令与脚本

| 工具 | 参数与默认 | 行为 |
|---|---|---|
| `run_command` | `command` 或 `argv` 二选一；`cwd="."`；`env={}`；`stdin_text?`；`background=false`；`wait_ms=10000`；`execution_timeout_seconds?`；`output_limit_action="preserve_process"`；`description?` | shell 文本使用固定可用 shell 的非登录模式；argv 直接执行，不重新拼接 shell；支持管道、重定向、多行和 heredoc |
| `run_script` | `path` 或 `code` 二选一；`language` 为 python、shell 或 node；`params_json?`；其余等待/超时/环境参数同命令 | code 写入版本化脚本工件；path 必须取当前工作区可读快照；参数 JSON 可走 stdin；使用预检过的解释器，复用命令内核 |
| `start_monitor` | 命令输入；`watch[]`；`description`；默认后台；执行截止缺省为 null | 长期进程；匹配、退出、失败产生事件；创建监视不自动创建持续处理委托 |

- `background=true` 接收持久化后尽快返回资源引用；false 只等待 wait_ms。等待超时不取消执行，也不再提交第二次。
- 执行 timeout 省略/null 表示无执行截止；显式正数才设置截止。仍有启动、网络调用和取消等待边界。
- 一次性 stdin 写入后关闭；没有 stdin 时用 DEVNULL。输入较大时有界流式写入，不占住 HTTP handler 或无限缓冲。
- 默认环境最小化；任务依赖环境通过路径/配置选择，不继承上一条独立命令的 shell 变量。
- 标准能力全部在 Kali 上实现；Windows 负责控制与纯协议测试，不承诺 Windows PTY。

### 6.2 资源查询与等待

保留 `get_job/read_job_output/cancel_job`，兼容旧参数，补充 `list_jobs` 与 `wait_for_resources`。

- 列表按 session 返回有界分页，字段包含资源 ID、创建 run、用途、状态、最近核对、退出信息、配额和值班状态。
- 输出读取指定 stream、cursor、limit，允许重读；不能共享一个会被任意消费者推进的隐式游标。
- `cancel_job` 与终端 close 使用持久操作 ID 产生取消请求身份，不让模型编造 UUID；保留旧 request_id 兼容但限制归属。
- `wait_for_resources(resource_ids, events, match=any|all, wait_ms=10000)` 只登记指定等待条件。一次工具等待到期后，Runner 可进入 waiting_tool；不能为等待反复空转调用模型。
- wait 条件是控制端领域执行意图；checkpoint 仅持有引用。长期资源本身不自动加入有限工作完成屏障。
- `unknown` 不是 succeeded/failed/cancelled，不能满足“已完成”条件；可通知待核对，但不据此自动重跑。

### 6.3 PTY 全部接口

| 工具 | 参数与行为 |
|---|---|
| `shell_open` | 命令输入、cwd、env；mode 为 interactive、hands_free、dispatch 或 monitor；`rows=24, cols=80`；`prompt_regex?`；`quiet_ms=8000`；`startup_grace_ms=15000`；`watch[]`；返回 terminal_id |
| `shell_send` | terminal_id；`text?`、`submit=false`、`keys[]`、`hex[]`、`paste?`；`wait=true`；wait_ms；可覆盖 prompt/quiet；执行端顺序化写入 |
| `shell_read` | terminal_id；view 为 stream 或 screen；cursor；`max_bytes=16384`；`max_lines=200`；有界返回 |
| `shell_resize` | terminal_id；rows、cols；原子调整 PTY 与终端模拟器，记录操作 |
| `shell_list` | 当前 session 的终端分页，包含交互是否仍可用；已退出的终端记录仍可查 |
| `shell_close` | terminal_id；正常终止并必要时升级，保留日志/回执；关闭重复调用不再产生副作用 |

四模式行为必须写在工具说明中：

- interactive：有界等首次可读输出/提示符/静默后返回，进程可继续运行。
- hands_free：创建后立即返回，显式读取或等待事件。
- dispatch：创建后返回，首次等待条件满足时记录通知；通知不是进程已成功退出。
- monitor：创建后返回，持续匹配 watch，命中不关闭资源；是否唤醒由有效委托决定。

输入构造顺序固定：text → submit 的回车 → keys → hex → paste。支持至少 Enter、Tab、Esc、Backspace、Delete、方向、Home/End、PageUp/PageDown、Ctrl-C/Ctrl-D。未知按键、非法 hex、非法 regex 全部在写入前拒绝，不能发送一半后才校验。

paste 采用 bracketed-paste 字节协议；必须注明目标程序可能不支持，不能宣称任何多行粘贴都绝不会执行。原始字节输入显式指定，不能静默转码。输入原文不要复制进普通事件，敏感数据的显示走现有脱敏机制。

单终端输入序号由服务端持久分配，并绑定操作 ID。同 ID 重试返回原回执，不再次写入。不同发送并发时串行；read/screen 不占用整个输入等待锁。

等待状态按本次输入后的输出边界判断，不能用上次残留提示符立即认定完成。prompt/quiet 只是观察信号，不是退出码。等待取消只停止本次等待，关闭终端必须走明确操作。

终端屏幕从 PTY 创建时开始有界维护，不在首次读屏时仅用截断尾部重建假屏幕；处理 ANSI 控制、光标、清屏、CR、中文宽字符及 resize。选择并锁定兼容 Python 3.12/3.13 的终端模拟依赖，用 fixture 验证；未通过时不能展示 screen 已支持。

## 7. 非交互任务包装进程：必须真正脱离服务生命周期

实现 `python -m workbench_executor.job_worker --job-id ... --spec-path ...`，由 Executor 以新会话启动。它不是模型服务或常驻 PTY 接管服务，只管理自己的一个任务及有限资源。

生命周期：

1. Executor 校验请求、session、fence、配额和参数，事务插入唯一操作及 job/resource 记录。
2. 执行规范以 UTF-8/权限 0600 原子写入数据根，记录内容哈希；命令原文不进入公开事件。
3. 包装进程对 operation/job 使用独占启动锁或同等原子 claim。并发提交、服务重启和重复启动不得造成两个实际命令。
4. 包装进程创建命令的独立进程组；保存 Linux host boot ID、PID、start_ticks、PGID、包装进程身份和开始时间。服务实例 boot_id 与操作系统 boot ID 必须分开。
5. stdout/stderr 使用非阻塞/并发排空的管道，由包装进程执行日志额度策略；不能直接无限重定向后依赖 Executor 定时看大小。
6. 包装进程保持截止计时、资源计数及取消控制能力；Executor 离线不影响这些职责。取消通过仅本机、受权限保护的控制通道或持久控制请求处理。
7. 取得真实 exit code 后，原子持久写完成回执及保留日志范围，再使状态可查询。回执包括 job、operation、spec hash、进程身份、开始/结束、退出码、终止原因和采集计数。
8. Executor 重启按台账及不可变回执核对；活着且身份匹配的包装进程可重新管理；结束且回执可信则投影终态；无足够证据则 unknown，不自动再次执行。

回执文件是不可变执行证据，SQLite 是对外执行台账；不能同时维护两份可任意修改的 job 真相。可由包装进程通过仅限执行记录的 Ledger 入口事务更新同一 SQLite，或由 Executor 从不可变回执幂等导入，必须定义崩溃窗口和单一写入职责。

数据目录建议 `jobs/<job_id>/{spec.json,start.json,completion.json,stdout.log,stderr.log,output-index.json}`。日志可分块，但原始区间映射必须稳定。执行规范和回执拒绝路径穿越、符号链接置换及明显身份不一致。

“收到请求但启动是否发生未知”不得自动重做。进程身份不确定不能 signal 猜测 PID；取消未确认返回 unknown。直接子进程退出不代表组内子孙全部退出，不能在早退路径虚报全部关闭。

## 8. 输出、日志与配额

### 8.1 输出页

OutputPage 至少含：resource_id、stream、cursor、next_cursor、原始字节区间、text、byte_count、at_current_end、process_exited、truncated、missing_ranges、encoding_error、artifact_refs。旧 eof 可兼容，但说明它仅代表当前读到末尾。

- 游标绑定资源/stream/offset，不能用于读取其他任务或任意路径。
- UTF-8 分页跨字符时使用增量解码或保留尾部未完成字符；游标按原始字节移动，不能漏字/重复字。
- 非法字节的原件在采集额度内保存，文本视图报告错误和原始区间。不能用 replace 后假称原始文本完整。
- 普通命令保留独立 stdout/stderr；PTY 为合流。需要跨流顺序时使用带序号与采集时间的块索引，不伪造程序内部精确时序。
- 不提供无界 drain；读取全量是分页请求，UI 和模型均有上限。
- live 日志不能冒充固定哈希工件；按不可变分块或快照导出 artifact，记录对应区间和 hash；结果文件缺失不能被解释为成功。

### 8.2 输出额度策略

`output_limit_action=preserve_process|terminate`，缺省 preserve_process；不可由任务类型自动选择。

- 达到额度后记录一次状态变更事件；preserve_process 持续排空并丢弃超额日志，记录缺失范围/可核实计数，不无限增长缺失元数据。
- terminate 才触发取消升级；记录明确原因，不能伪装用户取消或程序正常失败。
- 已保留日志不自动删；截图和回执等元数据有独立小额预算，避免输出日志占满全部容量。
- 内存环形缓冲、终端屏幕和 watch 检测可继续有界运行；若相关原件未保留，事件引用必须显示 unavailable/truncated。
- 高速输出必须有背压/排空设计和 CPU 边界，不能为每个字节生成数据库事件。

### 8.3 可配置工程初值

以下是本轮实施初值，不是任务总预算。配置应在设置、能力接口和文档中一致，测试用小配额替代：

| 配置 | 初值 |
|---|---|
| 工具交互等待 | 10 秒，单次最大 60 秒 |
| 单页输出 | 默认 16 KiB，最大 256 KiB |
| 单资源采集日志 | stdout/stderr 合计 64 MiB |
| PTY 内存回看 | 256 KiB，屏幕另有行列上限 |
| session 并行运行资源 | 16，其中 PTY 最多 8；Executor 全局 64 |
| session 采集日志预算 | 512 MiB |
| 磁盘低水位 | 剩余 1 GiB 时拒绝新建并告警，配置可调 |
| 取消等待 | TERM 宽限 10 秒，KILL 后确认最多 5 秒 |
| 屏幕尺寸 | 24×80；每边 1–500，限制总网格大小 |

并发额度只能拒绝新建，不自动淘汰已有资源。输出额度在多进程并发下使用事务预留/原子计数，不把竞争写超上限称为已强制配额。

采集日志、受控文件工具和任意程序自行写文件是三条不同路径：前两者必须真实限制；任意命令的文件系统访问在 trusted 模式下不受 cwd 沙箱保护。工作区总量做预检和持续计量，系统没有文件系统级 quota 时必须报告 `workspace_storage_enforcement=accounting_only`，不把巡检宣称硬隔离。磁盘不足错误必须可见；不能为保活无限写盘或删除既有证据。

## 9. 文件工具、脚本模板与环境

### 9.1 文件工具

保留现有 `list_files/read_file/write_file/edit_file`，命令与这些工具使用同一 `session_id` 工作区。新增：

- `edit_file_batch(path, expected_version, edits[])`：所有替换先在内存校验，任一失败不写文件；全部成功后临时文件+原子替换；保留现有版本冲突与唯一匹配规则。
- `glob_files(pattern, root=".", cursor?, limit=200)`：工作区内，支持 `**`，确定性排序和分页，拒绝路径/软链接逃逸。
- `search_files(pattern, paths?, regex=false, case_sensitive=true, context_lines=0, cursor?, limit=200)`：返回有界文件路径、行号和上下文；二进制/非法编码显式说明，不无界扫描或静默改码。

文件工具保留路径边界不代表普通命令也有系统沙箱。并发命令可以改变文件，编辑必须在写入前再次核对 expected_version；不靠控制端进程内锁假设全局安全。

### 9.2 可复用执行模板

实现 `list_execution_templates/get_execution_template/register_execution_template/run_execution_template` 或等价的小型目录与统一执行入口。

- 模板类型为 command 和 script，含 name、description、参数 JSON Schema、不可变 revision、内容 hash、解释器/命令结构、默认超时和环境需求。
- 注册/更新只是创建新版本，不直接执行。运行指定版本，operation digest 包含模板 hash 与参数。
- 命令模板优先以 argv 元素占位替换；shell 模板中的参数按明确规则引用，不能用原始字符串拼接。脚本参数走 stdin JSON，不能拼成代码。
- 模板只描述实际可执行能力；不把未经验证的模板写成已验证经验，不让模板安装系统软件或改变执行策略。
- 模板领域元数据归控制端业务记录，脚本正文归 artifact；Executor 接收本次不可变快照，统一走 command_runtime。

### 9.3 环境发现与依赖

`get_execution_environment` 返回 Linux/Python/shell 版本、可用解释器、配置的程序名称/路径/版本、代理/CA 是否配置（不返回秘密）、工作区依赖环境位置和缺失原因。

部署时准备基础解释器、shell、检索程序；动态发现从配置的名称与 PATH 定位，不扫描整台机器，也不在发现时自动安装。任务依赖可通过已有命令能力在工作区创建 venv 或本地 node 依赖目录，安装命令也必须有操作、日志及结果核验。系统级升级不是任务内默认行为。

最小环境允许 PATH、UTF-8 locale、任务 HOME/TMP、必要代理和 CA 配置；构造显式白名单并阻止管理/模型/数据库秘密。代理环境变量不等于全部流量已代理，不修改现有 HTTP/浏览器身份边界。

## 10. 持久事件与等待

### 10.1 执行端事件

SQLite 新增持久事件序号，不在服务重启后归零。事件至少含 executor_id、seq、event_id、session_id、resource_id、operation_id?、type、occurred_at、bounded payload、output_refs 和 availability。

事件类型覆盖资源创建、运行、退出、交互失效、watch 命中、取消请求/确认、日志截断、资源告警及恢复核对。实际输出是数据，不是指令。

`GET /execution/v1/events?after=...&limit=...` 从持久存储分页读取。每次接受操作/更新状态与关键事件应在同一台账事务中提交；已有 JSONL 可作为导出，不再作为与 SQLite 并行可写的事件真源。

watch 明确模式 literal/regex；限定长度、数量与检测窗口，处理跨读块匹配；避免不可控回溯阻塞采集。默认每条规则一次通知，可显式选择重复模式并设置去重/冷却。原始数据仍读取排空，通知合并不能假称没有发生事件。

### 10.2 控制端接收

- 轮询/订阅是 Runner 的基础设施职责，不消耗模型调用，不增加新的 Agent 循环。
- 以 `(executor_id,event_id)` 或等价稳定键唯一；同批事件落库成功后才推进远端读取游标。
- 控制端事件保存的是来源可追溯的投影；外部操作事实仍归 Executor。
- 按 session 分发；无活动 run 的保留资源也必须持续同步，不能只查询 waiting_tool run 名下的 job。
- 网络不通维持最后核对时间与 stale/unknown，不自动重做资源操作。

### 10.3 明确等待集合

wait-set 包含 session、归属 run、resource/event 条件、any/all、创建 fence 和完成状态。收到相关事件或核对结果后事务判断满足条件，去重唤醒。

有限 job 的未完成工作不能仅改称“保留资源”来绕过 finish_gate；是否依赖其结果由明确的计划/等待关系决定，阶段报告显示待办。交互终端和监视器本身不隐式阻塞。

## 11. 值班委托、取消和下次对话摘要

### 11.1 领域记录

新增专用 delegation 记录：delegation_id、session_id、resource_ids/event_filters、处理目标、mode=once|continuous、status、revision、授权来源用户消息/命令 ID、created_at/cancelled_at。不得加入一个万能可写状态表。

工具/UI 提供 `set_execution_duty/list_execution_duties/cancel_execution_duty` 或等价接口。仅当当前用户任务明确要求持续处理时才能建立委托；打开监视器本身不等于获得持续调用模型授权。工具输出、模板描述和终端文字不能作为授权来源。

### 11.2 状态行为

- 阶段结束且无持续委托：保留资源、同步事件，模型不自动启动。
- 有效委托匹配事件：当前 run 在推进时并入一致边界；正在等该条件时重新排队。
- 如关联 run 已是终态且委托明确持续有效，使用现有领域创建 run 路径建立同 session/thread 的后续 run；不能把 completed run 改回 running。遵守 session 单一活动 run 约束、模型设置、lease 及 token 记账。
- 新用户输入与委托事件并发时，只能获得一个活动 run，未消费事件留待该 run 聚合；不得启动两个 Agent。
- 明确持续委托是阶段结束默认不值班的例外，报告必须显示“持续值班中”；不能暗中保留委托。一次委托处理完成后结束，持续委托直到取消或明确终止条件满足。
- pause 优先，事件不自动恢复暂停。取消委托递增 revision，旧排队唤醒及依赖该委托的后续派发失效；不向资源发送 kill，不影响其他有效工作。
- 已经执行的副作用不能撤销；取消不假称已回滚。恢复已取消的委托需要明确的新动作，普通续聊只展示积压事件。

### 11.3 可靠聚合交付

控制端保存事件 delivery batch：稳定 batch_id、session、覆盖事件 ID/区间、摘要引用、状态和注入的稳定 message_id。摘要至少含资源用途、事件类型、首次/最后时间、次数、重要结果、缺失/未知和详情引用。

聚合可先采用确定性规则，不为每个事件增加模型摘要调用。预算有界，重要错误/退出/watch 状态优先；未装入本次摘要的事件保持 pending，不能因截断文本就标为已获知。

数据库和 checkpoint 不是分布式原子事务：先准备 batch，用稳定 message_id 注入并确认 checkpoint 持久化后确认 delivery；重启时按该 message_id 核对，不能只在内存生成摘要就推进水位。UI 已读维护独立状态，不消费模型交付位置。

## 12. 数据库与迁移

### 12.1 Executor SQLite

采用版本化、事务式、可重复运行迁移，至少覆盖：

- jobs 新增 session/run 归属、spec hash、进程/包装进程身份、host boot ID、控制 fence、输出策略和回执引用。
- 持久操作接收记录：operation_id 唯一、digest、action、resource、受理/执行/回执状态及最小结果快照。
- terminals 及输入操作序号/回执。终端记录是执行事实，不放图消息。
- 持久 execution_events 和控制 fence。
- 并发资源/输出预算的原子预留与释放，不能重复释放。

升级前检查历史 operation_id 冲突，发现歧义必须报告，不能删除旧记录强行加索引。历史无 session 的记录保留 legacy 标识，不允许模型凭猜测 ID 接管。

### 12.2 控制端 PostgreSQL

Alembic 增量迁移，利用现有 `ToolOperationRow/ToolJobRow/EventRow/ArtifactRow`。只增加必要专用领域表：资源只读投影、wait-set、delegations、源事件接收/消费位置、delivery batch、执行模板版本。

会话资源索引按 session，原创建 run/operation 不改写；跨 run 管理生成新操作而不是重写资源来源。方向变更保留旧方向警示。

图状态只引用 wait/delivery/delegation ID，不保存长日志、句柄或第二份可写资源状态。控制端与 Executor 间绝不直接访问对方数据库文件。

## 13. API、UI 与权限

执行端最小路由集合：

```text
POST /execution/v1/control                 同步会话 fence
POST /execution/v1/operations              接收命令/脚本/终端副作用
GET  /execution/v1/operations/{id}          按操作身份核对
GET  /execution/v1/resources               session 分页列表
GET  /execution/v1/resources/{id}           状态与实际能力
GET  /execution/v1/resources/{id}/output    有界流或 screen
GET  /execution/v1/events                  持久补读
GET  /execution/v1/environment             环境发现
```

写操作集中经 operations，避免 shell_send/close/cancel 各自绕过幂等。旧命令接口如保留，必须适配同一内核并完成归属/能力检查；缺少必要身份的旧请求明确拒绝，不能暗开无身份通道。

控制端在 `/api/sessions/{session_id}/execution/...` 下提供资源列表、状态、输出、单资源关闭、明确终止全部资源、委托列表/建立/取消和环境读取。复用 Cookie/Origin/CSRF 及现有命令受理机制；API 不驱动 Agent，也不把 stdout 全文塞 SSE。

UI 必须：

- 分开显示“本阶段结果”“待完成工作”“保留资源”。资源卡包含真实状态、交互是否可用、创建时间、最近输出/输入/核对、用途和配额。
- 分开显示资源状态、值班状态和 Agent 状态。按钮“取消值班”不能调用资源 close；按钮“关闭资源”必须显示取消是否确认。
- “终止任务”明确作用于当前对话：停止其 Agent 推进、取消其有效委托并核对关闭全部所属执行资源，包括早期 run 创建后保留的资源；不能只取消当前 run 的 job 而留下隐形资源。“暂停”则不关闭这些资源。全部关闭未确认时仍展示未决清单。
- 提供 stdout/stderr/PTY stream 分页与 screen 快照，查看日志缺失和证据不可用说明。
- 阶段结束和网页关闭不清理资源；无输出只提示。报告列出仍保留的资源，不把它们全部称为任务未完成。
- 人工直接向 PTY 输入不是本轮必需交付；Agent 交互、用户查看、取消值班、关闭资源与终止任务必须完整。
- 用户删除对话时不可直接删除仍存活资源的台账：按已有删除语义增加资源预检，要求明确终止或保留可管理的归档记录，不能制造孤儿。

## 14. 部署、恢复与兼容回退

编码智能体准备部署与演练代码，不自动重启用户正在使用的 Kali 服务或启动真实目标。独立 Linux 临时数据根中的自测允许；最终真实部署由验收智能体执行。

必须支持：

- 本机依赖、协议生成物、前端构建；Kali wheel 包含新模块和协议，不依赖控制端源码 import。
- 执行端 Python 3.12/3.13 基线，实际安装依赖有清单和版本，不传模型 Key/数据库凭据。
- 更新前检查活动租约、运行 job、存活 PTY 和暂停资源；活动用户工作存在时不强行重启，报告具体阻碍。
- 服务停止只停止服务。systemd 默认 cgroup 回收可能杀掉独立会话子进程，不能仅凭 setsid 宣称存活；部署脚本与 service 模板必须有明确且经演练的独立任务策略，不支持的管理模式标不可用。
- 更新新版本后核对 source/wheel/installed package 哈希、PID/start time、实际导入路径、health 与 capabilities；不是上传文件完成就算进程加载新版。
- 原桌面 `Rexak Agent` 入口保持 `start-workbench.cmd → scripts/start_workbench.py` 链路，验证加载本仓库新版本。
- 旧数据库增量升级、不清空运行数据；先备份。回退先禁止新创建、核对活跃资源及 schema 兼容，不盲目运行 downgrade，不删除证据。

## 15. 编码顺序（中间不提交）

1. 保存工作区基线和未提交路径清单；阅读 AGENTS、最新 decisions 和涉及模块；记录需新增的决策。
2. 实现协议真源、生成/兼容、数据库迁移及可用性报告，建立 contract tests。
3. 实现有界 output_store、独立 job_worker、command_runtime、幂等和恢复；通过真实 Linux 进程演练。
4. 接通工具层与共享工作区、run_script、文件检索/批量编辑、模板和环境。
5. 实现完整 PTY、输入回执、四模式、screen、resize 及失联核对。
6. 实现持久事件、wait-set、委托、撤销、恢复、续聊聚合和 finish_gate。
7. 接通 API/UI、类型生成、报告与取消行为；用确定性模型完成端到端演练。
8. 完成部署更新和独立验收脚本、运行说明、变更来源/依赖清单及自测报告。
9. 执行全部可用检查，修复后冻结工作树；按验收文档交付一次 commit。

步骤间可以迭代，但不能把“只完成命令工具、PTY 以后再做”作为完整交付。

## 16. 交付定义

最终交付必须同时包含：完整能力代码、控制端/Executor 增量迁移、协议生成与漂移检查、锁文件、测试/故障演练、API/UI、运行与部署说明、自测证据索引及唯一 commit。

自测证据按验收编号逐项列 PASS/FAIL/NOT_RUN、命令、环境、结果位置及限制。仅文档承诺、mock 进程或 Windows skip 不足以证明 Linux 执行能力。Linux 环境确实不可用时准确记录 NOT_RUN，不用“全部通过”遮掩。

验收智能体独立复核和执行部署；开发者只报告“实现与自测结果”，不替验收者宣告最终通过。任何真实目标或真实模型效果结论不属于本次工程验收。

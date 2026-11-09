# 最近暂停 Pwn Run 排查与最小修复

日期：2026-11-08。对象为 Run `b5bd9266-b8af-45fd-a951-2f056afbaa02`，Session `1d571c85-934f-41f8-b9f5-9aa3b8aff672`，北京时间 18:20:28 创建，18:21:26 暂停；与截图的 pwn5 复测、HTTP scope 拒绝及并发 fence 冲突对应。

**结论：确认三个工程遗留问题，已最小修复并重新加载 Windows 服务。** 不是 Pwn 工具包未安装；本轮附件交付、解压、checksec 已实际成功。此前 READY 报告验证了部署和命令可达性，没有覆盖裸域名目标输入及同轮六路并发控制同步，这些缺陷因此漏过。不能将“部署验收通过”解释为真实任务行为已无缺陷。

本次仅只读核对原数据库/事件/结果，并进行离线定向验证和必要 Windows 重载。没有恢复题目、执行附件、连接目标、调用模型、重跑 CTF、修改 Prompt、A2A、工具 Schema 或授权边界，没有 commit/push。

## 1. HTTP 两次被错误拦截

原始用户目标明确写了 `目标地址：node5.anna.nssnode.cn:27571/`，但 Session 持久 scope 为 `mode=instruction / revision=1 / rules=[]`。

HTTP 在事件 seq 16、30 均返回 scope_denied、execution_certainty=not_applied、空 Job ID。也就是说请求被控制端预检阻止，根本没有交给 Executor HTTP 执行；不是 HTTP 服务崩溃、Python 包缺失或远端请求超时。

根因在 `domain/instruction_scope.py`：原识别表达式只识别带 http(s) 的 URL 和裸 IPv4，没有识别用户明确提供的 DNS 域名端口。换成 curl 只是走到另一种执行方式，不能修复错误的范围投影，也不应据此宣称 HTTP 工具本身不可用。

修复：支持用户原文中带明确目标上下文或独立一行的 `域名:端口/路径`，仍排除禁止/示例、无关正文、邮箱/路径，并校验端口有效性。不从模型回复、工件或工具输出增加授权；严格 scoped 模式保持原规则，其他域名不因此获准。

已用本次真实原始指令离线验证：提取出 `node5.anna.nssnode.cn`，同主机 HTTP/HTTPS 可通过原范围策略，无关主机仍拒绝。没有向目标发送请求。

**没有直接修改这个暂停 Session 的 scope。** 修复已在新服务中可用，下一次用户显式继续时，Runner 的既有 `sync_instruction_scope` 会从原始用户指令补齐规则；不需要用户重新手填 scope，也不自动恢复当前题目。

## 2. 三次并发 fence 冲突

18:21:00.809–18:21:01.028，模型在同一轮提出六条 run_command，其中包括重复的 HTTPS curl 和相近 strings 命令。三项在控制同步阶段失败：

- `ccdb282f-646e-40ad-ac64-e4b42f2549d6`。
- `afb01771-269f-42ad-9193-bbe3c588f91a`。
- `11915289-57e2-4b0e-b951-951aec1c5355`。

错误均为 `expected_seq 落后于执行端当前 fence`，没有 Job，明确尚未派发。不是这三条命令在 Kali 上执行失败。

根因：同一 Run 的并发工具都同步相同控制状态，Executor 的正常 CAS 更新推进 seq。控制端原先遇到 seq 冲突只机械重试一次，并发请求继续推进 seq，导致无权限变化的同一控制状态也被报错。

修复仅在控制端 `tools/execution_dispatch.py`：如果拒绝原因确为 expected_seq 冲突，且远端回执中的 Session、Run、owner、generation、control_version 完全一致，并覆盖全部待同步撤销，则复用已经生效的 fence，不再争抢同一个 CAS 更新。运行时 seq 提示不因乱序回执回退。

不同控制版本、Run、owner、generation、其他拒绝原因仍不能被当作等价成功；新增撤销不能被漏掉，远端更高撤销版本保持。没有重放执行操作、放宽 Executor 检查、改变 operation_id 或序号校验协议。

离线测试使用真实 Executor Ledger 的独立临时 SQLite，通过六路并发实际复现 CAS：修复后六条同步均成功，只有六次同步调用，不再出现额外争抢重试。没有使用生产台账或执行目标命令。

## 3. 多余 `</think>` 漏到显示层

原始模型输出包含连续两个关闭标签。原显示投影只去掉第一个完整 `<think>...</think>`，第二个关闭标签被当作正文显示，正是截图中的现象。

修复 API 和前端的显示投影：去除正文开头残留的关闭标签，并处理流式半截关闭标签；普通正文中、引用或代码块中的示例仍保持。没有修改原始模型回复、事件、checkpoint 或工件。

部署后对这道题的真实只读 conversation 接口核对：HTTP 200，5 条可用 Assistant 记录的预览均没有开头的 think 控制标签。流式显示另有专项测试；页面刷新后使用新 JS。

## 4. 其他现象的归类

|现象|本轮证据与判断|
|---|---|
|Pwn 工具可用性|附件交付与解压成功，checksec 实际输出 ELF 保护；没有证据表明统一工具 PATH 回退或 checksec 不可用|
|HTTP curl 退出 56|实际命令启动，返回码来自 curl；不是 scope 拒绝。使用 -s 且未加 -S，会抑制错误文本，不能把没有正文当作没有执行|
|两条 HTTPS curl 退出 1|确已启动，但输出只有命令自己的标题；没有充分证据唯一确定根因，未重连目标追查|
|strings 管道退出 141|已有实际输出，命令经过 grep/head；与下游截断导致的 SIGPIPE 相符，是严格 pipefail 下的命令结果，不是工具缺失。未关闭 pipefail 或将非零伪装成成功|
|后续 TCP/TLS 检查|原保存输出有 TCP connected、TLS 握手/证书信息；说明当时并非完全无法连接。自签/过期证书是目标输出；curl 使用 -k，不能单凭证书警告归因其退出码|
|重复命令|同轮确有重复 HTTPS 与近似 strings 工具调用。它们是不同 ToolCall，不能擅自合并成同一个逻辑操作或把模型重复探索都归因于幂等失败|
|共享 /tmp/pwn5|模型自行选用了共享临时目录；本轮未取得跨任务污染证据，不标为已证实故障，也未清理或重写其中数据|
|Pi Worker|本次 58 秒运行尚未发起 Pi 委派，没有证据将上述失败归因于 A2A 附件交接或 Worker 路径|

本轮共有 11 条持久化 run_command：8 条实际 Job（3 成功、5 非零退出），3 条在 fence 同步前置阶段拒绝；另有两次 HTTP 范围前置拒绝。不同层级失败分开记录，不将所有 failed 当成一个工具环境问题。

## 5. 修改与验证

修改文件：

- `src/agent_workbench/domain/instruction_scope.py`、`tests/unit/test_instruction_scope.py`。
- `src/agent_workbench/tools/execution_dispatch.py`、新增 `tests/unit/test_execution_fence_coalescing.py`。
- `src/agent_workbench/api/assistant_display.py`、`tests/unit/test_assistant_display.py`。
- `web/src/console/assistantDisplay.ts`、`web/src/console/assistantDisplay.test.tsx`。

定向结果：

|检查|结果|
|---|---|
|范围识别专项|5 passed，包括裸域名、范围隔离、显式排除、无关文本与无效端口；首次发现端口 0 被既有 parse_target 默认成 80，修复后仅复测此文件|
|fence 专项|8 passed，包括真实临时 SQLite 六路 CAS、不同控制身份拒绝、新增及更高撤销版本|
|API 显示专项|15 passed，原有案例加重复/残留关闭标签|
|前端单文件专项|11 passed，包含流式每个前缀的显示保护|
|相关 Ruff / Pyright|通过|
|前端构建|一次成功，必要产物更新；原 chunk 大小提示保留，不扩大重构|
|仓库编码|默认范围 785 文件通过；本报告和辅助脚本另按 UTF-8 无 BOM 规则检查|

没有全量回归、额外模型实验、目标扫描或重复 Pwn 工具链验收。

## 6. 实际部署状态与边界

重载前再次确认无活动控制端 Run、有效租约、Worker 请求或 Executor 资源。备份原服务日志和前端构建后，使用原 Ctrl+Break 清理 API/Runner/Worker；旧批处理留在 pause 的外壳在核对 PID/创建时间、确认无服务子进程后单独关闭，没有按名称批量终止。

一次必要前端构建后，通过原 `start-workbench.cmd --no-browser --no-recover` 重启：API PID 17236，Runner PID 41256，Pi Worker PID 27080，分别在北京时间 18:38 启动。原 launcher 已确认各组件就绪。旧任务自动恢复禁用，模型配置未切换。

新页面 JS `index-qXe6I-S-.js` 与磁盘逐字节一致，SHA256 为 `c43fc44334983876d3a7727141cec5322f506bf3ad084a6ac0853c44bfb1b5ca`；历史对话投影检查已通过。

Kali Executor 源码没有变化，27 个文件实际安装哈希与本机一致，保留 boot ID `3485002-2470199d96ea`，没有重启或重装工具。

这道题仍为 paused，updated_at 保持 `2026-11-08T10:21:26.569976+00:00`，历史 scope 和原始记录未修改。没有替用户继续题目或重放失败操作。

尚未验证：真实目标是否提供标准 HTTP 服务、继续后的模型策略与 CTF 结果、修复在新一轮真实多工具调用中的表现。本轮确认的是离线根因复现、最小修复及实际服务加载，不能宣称已经重新完成题目。

## 7. 证据

- `work/latest-pwn-evidence.json`：原始只读 Run/Session/Operation/Event 快照；包含原始模型数据，仅用于本机取证，本报告不复制隐藏分析或附件私钥正文。
- `work/latest-pwn-fix-tests.log`、`latest-pwn-scope-retest.log`、`latest-pwn-fence-final-tests.log`、`latest-pwn-display-test.log`：必要定向验证及修复后复测。
- `work/latest-pwn-live-state.json`、`latest-pwn-reload-stop.json`、`latest-pwn-reload-wrapper.log`：空闲检查、日志/构建备份及受控清理。
- `work/latest-pwn-launcher.log`、`latest-pwn-windows-after.json`：新服务启动。
- `work/latest-pwn-verification.json`：原指令离线解析、新页面哈希、真实历史对话显示及暂停状态未变。

没有 commit/push。

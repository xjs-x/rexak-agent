# 工具效率整改：交接给缓存工作

日期：2026-11-07。请在当前工作树上继续，保留双方未提交修改，不整文件覆盖。

## 本次已完成

1. **路径信息**：附件回执增加 `path_base/command_cwd/resolved_path`；命令资源从原 Executor spec 投影 `execution_context.cwd/workspace_root/strict_shell`，可重查实际目录。
2. **环境发现**：复用 `get_execution_environment`，增加常见分析程序与最多16个按需程序名称、`observed_at`，版本失败标记未核实。首次 Main 诊断执行自动采集一次；显式工具可刷新。只查 PATH，不全盘扫描、不自动安装。
3. **失败及产物**：Main `run_command/run_script` 新增 `strict_shell`（默认true）和可选 `expected_files`。Shell 使用 Bash errexit/pipefail，argv/Python/Node不改变语义。结束回执提供独立 `stderr_output`；`resource.diagnostics` 区分产物缺失/空/非空/越界。真实退出码与产物检查分别表达，不能把产物失败篡改成进程失败。

协议缺省 strict_shell=false 保留旧操作包语义；Worker 原生工具、监视器、终端未强制改模式。新增能力 `analysis_diagnostics`，旧端不允许静默忽略新诊断要求。见 decision 0025 和 tool-efficiency-repair-20261107.md。

## 缓存工作需要衔接

- `agent/prompts/system.py` 已更新到 **PROMPT_VERSION=20**；新增三条路径/环境/诊断规则。缓存提示布局调整必须保留这些规则。
- 工具 Schema 已改变：`run_command/run_script` 的两个参数，以及 `get_execution_environment.tool_names`。缓存键/Schema指纹应反映实际生效工具，不能沿用旧缓存声明。
- `tools/runtime.py` 新增 `Toolbox.execution_environment_snapshot`，是 **Run内只读环境快照**，不是模型响应缓存，也不是新的权威状态表。
- `agent/middleware/working_context.py` 将该快照的 executor_id、observed_at、tools、missing、interpreters 投影到 working_context.execution_environment。采集后每轮复用同一 observed_at，不要每轮刷新时间破坏前缀稳定性；安装或环境变动后通过工具刷新，不得为了命中率继续使用已知失效事实。
- 缓存布局若将静态内容移到前缀、动态内容移到尾部，应完整保留上述环境投影及路径/诊断结果；不要通过删除字段、截断来源或改写历史 ToolMessage 来追求命中。
- `execution/client.py` 的 execution_environment 新增可选 tool_names 参数；旧无参数调用仍可用。`tools/commands.py` 首次诊断执行会读取环境并存入 Toolbox，后续模型轮次可见。
- 协议真源在 `contracts/execution_runtime.py`，执行端副本由生成脚本维护；不要手工改生成物。stdout/stderr与diagnostics仍来自原执行台账/工件，不作为供应商缓存统计来源。

## 当前验证与待续事项

- 控制端协议/执行摘要/门控及桌面入口专项 **50 passed**；执行端同一组 **8项在Kali通过**，另有 **6个隔离场景通过**。未跑全量、未调用真实模型/靶场。
- Kali 已安装新版本，**26个文件哈希一致、新启动身份核验通过、active_jobs=0**。后端通过原 start-workbench.cmd 使用 --no-recover 启动，历史9个非终态Run和凭据保持，模型调用数未增加。
- 修改范围 Ruff、编码和生成协议检查通过；本次 execution_environment 的定向 Pyright 通过。
- 最新全局 **Pyright 已通过（0 errors）**，此前上下文消息列表类型问题已不再出现；**最终前端构建仍未通过**：TaskDrawer.tsx:305、Workbench.tsx:317 报 unknown 不能赋给 ReactNode。这些位于并行缓存/统计改动范围，尚未替另一项工作修改，请继续修复后再统一验收。
- 因此控制台目前使用此前成功构建的 dist；不可将后端启动通过理解为当前全部前端源码已构建交付。

私有验收证据在用户目录 `.workbench/tool-efficiency-*.json`；最终部署证据为 tool-efficiency-deployment-final.json，隔离验收为 tool-efficiency-smoke.json。双方代码均未提交或推送。恢复缓存工作后，请重新核对实际工作树及运行进程，避免旧进程/旧前端冒充最终版本。

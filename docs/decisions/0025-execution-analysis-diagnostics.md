# 0025：执行路径、环境发现与分析产物诊断

日期：2026-11-07。

- 附件交付回执给出 path_base、command_cwd、resolved_path。命令回执/资源状态从 Executor 的持久 spec 投影实际 cwd、workspace_root 和 strict_shell；不根据模型描述猜测路径。
- 复用 get_execution_environment：默认检查常见分析程序，也允许最多16个程序名称的按需发现。只查询 PATH，不扫描整机、不自动安装；版本未核实显式标注，Java 类工具读取 stderr 版本信息，失败的版本命令不冒充版本。返回 observed_at，控制端在 Run 内保存只读快照供后续轮次使用；首次诊断执行自动读取一次，环境改变后显式刷新。
- 新 Main run_command 和 Shell run_script 默认使用 Bash errexit/pipefail；协议参数 strict_shell 缺省 false，以兼容原始/旧操作包。argv、Python、Node 不改变执行语义；监视器和交互终端保留原语义。Main 可显式关闭严格模式，预期非零退出宜使用条件分支处理。不是 Bash 时明确拒绝严格模式，不静默降级。
- expected_files 是可选的、相对 cwd 的产物声明，最多32个；受理前核对工作区边界，完成时再次核对符号链接和边界，只读取文件元数据。completion.json 保存检查事实，资源状态投影它；运行中/旧任务缺少检查不能当作通过。
- 产物检查与真实退出码分别表达。退出码0但空/缺失产物时，进程执行仍 succeeded，diagnostics.output_validation 为 failed，控制端摘要明确提醒。非空不证明新生成、语义正确或漏洞成立。
- 回执独立提供有界 stderr_output，完整输出仍走既有日志/工件机制，不增设第二套执行状态台账。新增能力 analysis_diagnostics；请求该能力时旧端必须拒绝，不能静默忽略。

严格 Shell 也不能识别所有人为掩盖的失败（如显式 || true、主动禁用检查、程序内部吞异常）。本决策提供通用执行事实和检查，不宣称消除所有模型误判，不改结束判定，不自动重放未知操作。

# 工具效率前三项整改

日期：2026-11-07。

范围：附件/命令的路径基准、可信工具环境发现、执行失败与产物诊断。图维护和委派策略未纳入本轮。

## 实现

1. 附件回执增加 path_base、command_cwd、resolved_path，区分 Session/Worker 工作区。命令资源从持久 spec 给出实际 cwd/workspace_root，重查也可定位。保留原相对路径、权限和稳定操作身份。
2. 扩展既有环境发现，检查常见编译/分析程序，可按需补充最多16个程序名称；不接受路径/Shell 文本，不扫描整机。Java 等从 stderr 核对版本，非零退出与未知版本不报为已验证。记录时间，Main 首次诊断执行自动采集一次，后续上下文保留只读快照；显式发现可刷新。
3. Main 命令和 Shell 脚本默认 errexit/pipefail，独立返回 stderr。提供 expected_files 声明与完成时非空检查；越界、空文件、缺失、不可读分开表达。检查保存到 completion.json，由资源状态读取，不增设可写镜像台账。退出码与产物有效性分开：进程0退出而产物检查失败时明确警示，但不篡改退出事实。

原协议缺少 strict_shell 的旧包保留 Shell 语义；argv/Python/Node、Worker 原生工具、监视器和交互终端未强制改变。严格模式只承诺 Bash 的标准错误传播语义，不识别所有程序内部吞错或显式忽略错误；产物检查可选，也不证明文件新生成或内容正确。技术约定见 decision 0025。

## 专项证据

- 控制端协议/摘要/执行门控及原入口部署：50 passed。新增检查覆盖“产物失败不能篡改进程退出码”。
- 本机执行端专项：最终6 passed / 2 Windows skip；相同8项在真实 Kali 全部通过，含真实 Bash 和符号链接。
- Kali 独立临时 Executor：6 个专项场景通过，包含缺少命令/管道失败及 stderr、空/缺失/正常产物、实际 cwd、资源重读诊断、重放复用身份、越界拒绝、环境按需发现与非法名称拒绝。仅合成命令，未访问靶场/模型；临时测试目录保留。
- 修改范围 Ruff、编码、生成协议漂移校验通过。没有跑全量回归。
- 最新全局 Pyright 已通过（0 errors），此前上下文消息列表类型问题已不再出现。最终前端构建仍受到并行工作区修改影响：TaskDrawer/Workbench 出现 unknown→ReactNode 错误。本次保留并行修改，不宣称全仓检查通过。前端首次构建通过，但该证据不能覆盖随后其他修改。

## 部署与数据保留

Kali 更新并重启，26个已安装源码文件、新进程身份和能力核验；原桌面快捷方式保持指向 start-workbench.cmd，本机通过该入口使用 --no-recover 启动 API/Worker/Runner，避免恢复暂停任务。健康、控制台和 OpenAPI 均为200。执行端 active_jobs=0。

前后9个历史非终态Run（6 paused/3 recovering）快照与凭据摘要保持；没有新模型/靶场实验。私有证据：用户目录 .workbench/tool-efficiency-before.json、tool-efficiency-after.json、tool-efficiency-deployment-final.json、tool-efficiency-smoke.json 和 tool-efficiency-launch.log。

未提交或推送 Git。保留工作区原有以及并行修改。

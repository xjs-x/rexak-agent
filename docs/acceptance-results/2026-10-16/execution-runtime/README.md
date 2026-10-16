# 命令执行底座：工程与部署验收

本轮在 `C:\xjs\rexak-agent` 的既有部分实现上继续落地。开工 HEAD 为 `a3d9df06fa804374fc24c77c5f2140fe7e79146c`，分支为 `codex/implementation-v1`。运行时不依赖外部参考项目。

## 结论与范围

命令、脚本、持久 PTY、屏幕、监视、文件编辑/检索、环境预检、模板、明确等待、资源管理和事件交付已接入现有 Python 架构。已完成本机工程检查、独立 Linux 真进程/PTY 演练、Kali 安装重启与哈希核对，以及原桌面入口和资源面板验证。真实模型及真实靶场测试未执行，由用户负责。

这不是“64 项的所有故障排列均已验证”的声明。[逐项索引](matrix.md)记录实际检查及覆盖边界。最后补齐 H06：在隔离副本运行旧二进制，旧 HTTP 能力仍可读取，新执行协议不可用；恢复新版后核对同一后台资源、连续日志和单次启动标记，并确认取消。增量升级、备份完整性和隔离还原也已验证。本轮工程与部署验收通过，可用原桌面入口使用。

## 已落地行为

- 指定 Kali 显式启用 `trusted`，无需逐条审批；发行默认 `disabled`。不承诺系统级出站隔离或文件系统硬配额。
- 非交互任务由脱离服务生命周期的包装进程承载，日志和完成回执可跨服务重启核对。PTY 重启后明确报告交互丢失，保留历史，不自动重建。
- 资源用途与启动方式分开，命令启动的监听器也可作为保留能力。有限工作与明确等待仍影响阶段完成。
- 阶段结束保留资源，展示保留清单、状态及输出额度；无空闲自动清理。关闭资源、暂停、终止任务、取消值班分别处理。
- 资源存活不代表 Agent 值班。默认只记录和展示事件，下次对话聚合交付；明确的 once/continuous 委托才触发唤醒。撤销委托不关闭资源。
- 操作 ID、输入序号、事件序号、委托 revision、控制 fence 和稳定 delivery message ID 保证可核对恢复；无法证明未执行的窗口返回 unknown，不自动重放。

## 证据与批次

工程检查的最终计数见 [local-checks.json](local-checks.json)，完整控制端输出见 [final-control-tests.txt](final-control-tests.txt)。最终为 **601 passed / 2 skipped / 1 real_model deselected**。两条 skip 中文原因在终端转码时丢失，按测试源码重建并在日志末尾标注；计数及其余输出保留。开发自测命令和代码归属见[开发自测报告](../../execution-runtime/developer-selftest.md)。

Linux 执行端完整批次为 **92 passed**，见 [all-linux-tests-ported.txt](all-linux-tests-ported.txt)。后来补充的恢复窗口及真实 PTY 输入故障批次为 **11 passed**，见 [extended-linux-pytest.txt](extended-linux-pytest.txt)；与完整批次存在重叠，不能相加。旧 fixture 的固定回环端口在隔离副本替换为随机端口，未占用用户端口。

独立演练脚本累计 **31 个案例通过**，见 [linux/results.json](linux/results.json)。这是带来源批次的合并索引，不是一次全量运行：首次 29 PASS / 1 FAIL，E04 暴露 PTY 输出索引未及时持久化；修复后相关 5 项复验通过，再补 4 项并发/预算/低磁盘演练。初始失败记录保留在 [acceptance-final](linux/acceptance-final/results.json)，修复批次见 [final-fixes](linux/final-fixes/results.json)，扩展批次见 [extended-cases](linux/extended-cases/results.json)。

前端确定性 API fixture 演练覆盖用途分组、输出翻页、交互丢失、取消值班不关闭资源、关闭未确认和浏览器异常检查，见 [UI 结果](ui/ui-results.json)。实际桌面入口截图为 [desktop-execution-panel.png](desktop-execution-panel.png)；截图仅使用专属 fixture 会话。

## 部署验收

[deployment.json](deployment.json)记录 Kali release、wheel 哈希、26 个上传文件和 21 个安装源码哈希、依赖检查及服务重启前后 boot ID。实际导入安装包路径和再次源码哈希核对见 [final-remote-verification.json](final-remote-verification.json)。后续控制端修正未改变已部署 Executor 源码。

部署后只读核对 [H03](deployed-readonly/results.json) 和专属无害 fixture 冒烟 [H05](deployed-smoke/results.json)均通过。原 `Rexak Agent.lnk → start-workbench.cmd → scripts/start_workbench.py` 链路保持，控制台加载的 JS/CSS 与新构建逐字节一致，见 [desktop-launcher.json](desktop-launcher.json)。最终再次从原命令入口运行 API-only 短时启动，未启动 Runner；见 [final-launcher.txt](final-launcher.txt)。

更新前核对活动任务，保留凭据、日常数据和暂停状态。备份 `backups/20261016T005451163610Z` 核对 2857 个文件，隔离还原通过。测试遗留包装进程仅在专属 fixture 身份、完成回执或无子进程得到核对后清理，未处理用户进程。

受限回退脚本为 `scripts/accept_execution_runtime_rollback.py`，使用本仓库开工基线的源码导出，不依赖外部项目。[rollback/results.json](rollback/results.json)记录活跃非交互资源身份保持、日志继续、只启动一次及取消确认；基线源码逐文件哈希见 [legacy-source-manifest.json](rollback/legacy-source-manifest.json)。旧版期间不接管新资源，不把快照覆盖回正在被包装进程写入的 SQLite；恢复新版后继续管理。活跃 PTY 不在这个回退能力内。

## 修正与未测边界

复验期间修正了规范摘要与文件摘要混淆、脚本 stdin/解释器解析、PTY 启动与输出索引、进程组残留、并发首次资源登记、wait-any 与延迟唤醒、取消委托竞争，以及 delivery 必须等 checkpoint 回执才能确认交付等问题。相关回归保留在源码测试中。

未执行真实模型/靶场、机器重启自动拉起、所有故障时序排列及日常服务的生产降级。PTY 的交互跨 Executor 重启恢复、活跃 PTY 降级、硬文件系统 quota 不是本版能力；UI/能力声明如实显示边界。测试通过仅证明所列工程行为，不证明真实测试效果。

`source-manifest.json`记录最终暂存代码 blob 与 SHA-256，`evidence-manifest.json`记录本目录证据哈希；两者排除各自及相互自引用。最终 commit SHA 在交付回复中给出。

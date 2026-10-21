# 动态规划闭环修复：P0 / P1

日期：2026-10-21。基于当前未提交的第一轮实现继续修复；决策见 [0013](../../../decisions/0013-planning-loop-repair.md)。P2 真实 API Key、模型及 Tomcat 回归由用户执行。

## 实际交付

- 同会话有效结果自动跨步骤复用为 supporting，记录真实操作链，保留原执行归属。
- HTTP 完成按声明的方法、端点、可选身份/认证方式核对真实回执。OPTIONS 单一响应不能完成声明包含 PUT 的检查。来源真实性、适用性和结论充分性的检查范围分别保存。
- 授权、策略、身份、显式时间与依赖条件检查；只新增条件不使原条件自动失效。历史元数据不足显示限制，不升级为独立验证。
- 逐步骤反馈错误，支持一次修正多个问题；整体提交不变，无关步骤可只列 step_id 沿用。不重放外部操作，不伪造或覆盖程序检查。
- 关键增项、切换、受阻等在同次更新用四个简短字段记录判断，程序补齐旧焦点和旧版本。普通调用和单纯完成登记不要求 Assessment。
- WorkingContext 呈现已有结果、原焦点、工件入口与待收尾工作；不新增工具调度器或可写黑板。
- 精确记录新增/移除/实际变化及相对顺序；Run 装配前版本为 unbound，装配后记录已加载提示及有明确范围的代码指纹，保留激活历史。
- API/UI 接入新检查范围，State 6 与旧 Completion 读取兼容。未改变的历史完成可随计划保留，FinishGate 及归档仍要求复查。

## 验证依据

新增 `test_planning_repair.py` 使用隔离 PostgreSQL、真实工件和官方图。覆盖自动来源复用且原操作不变、HTTP 方法范围不足、阴性调查、批次原子性、逐步骤错误、目标/身份/认证/时间/策略/来源限制、条件补充与撤回、关键修订简写、精确变更、结果目录、历史完成复查和运行版本。

官方图用本地有计数回执的 HTTP 替身，一批三个调用继承同一焦点，完成两项不同范围的调查；每个工具边界关闭连接并重新构建图，最后形成结束提议，外部逻辑调用仍为三次。模型为脚本替身，不调用供应商，不访问目标；该结果验证协议而非真实模型决策质量。

既有独立进程 checkpoint/投影恢复和暂停取消回归继续运行。最终完整回归为 **730 passed、2 skipped、1 deselected**，耗时 199.20 秒；JUnit 共 732 项，0 errors、0 failures。单元 319 passed / 2 skipped，集成 369 passed，恢复 42 passed。新增修复文件含 20 个集成用例，另新增 4 个单元用例。

两项跳过仍为 Windows 符号链接权限；真实模型实验排除。28 条既有 Pydantic 测试替身序列化警告保留。早期回归发现旧测试仍要求拒绝跨步骤来源、并要求执行上下文没有新增快照；断言已改为检查自动支持及原字段原样保留，新增条件快照另行断言。失败尝试不计为通过。

冻结依赖同步（91 包）、Ruff、Pyright（0 errors / warnings）、UTF-8 无 BOM 检查（521 文件）、OpenAPI/前端类型生成、前端类型检查与生产构建、git diff --check 均通过。

最终回归命令：

```powershell
uv run pytest tests/unit tests/integration tests/recovery -q -p no:cacheprovider -m "not real_model and not real_executor" --tb=short --junitxml=data/runtime-precontract-20261021/acceptance/2026-10-21/planning-repair/controller-tests.xml
```

## 桌面入口与 Kali 验收

- 更新前无活动租约、无待消费指令；已有任务分别为 cancelled 和 paused。数据库和工件备份完成，412 个文件校验通过。旧启动日志单独归档，不覆盖 Tomcat 实测日志。
- 正常停止旧入口进程，再从原桌面 `Rexak Agent.lnk` → `start-workbench.cmd` 启动。API 与 Runner 就绪，二者实际提示版本均为 11，已加载规划指纹一致，为 `loaded-planning:fae5db98dd9f982bc7226dc288c9ff23e3116f6b`；健康检查及 Runner 启动日志分别提供证据。
- 实际 API 包含新 http_checks/source_links 合同；页面返回的 JS/CSS 与当前 dist 哈希一致。新进程保留运行，供用户开展 P2。
- Kali 的 23 个已安装 Python 源文件及 executor-control.py 与本机一致；身份、协议 1 和能力核对通过，活动操作为 0。Executor 本轮无变更，未重复安装或重启，前后启动身份相同。
- 37 张业务、checkpoint、Store 表的行数和内容摘要前后一致；凭据配置文件哈希一致。已有 Tomcat 会话事件序号仍为 588，暂停状态保持，模型调用总数仍为 40，没有新增真实调用或目标操作。

原始证据位于实际数据根 `data/runtime-precontract-20261021/acceptance/2026-10-21/planning-repair/`：测试 XML、deployment/before.json、after.json、launcher-api.json、kali-source-check.json、kali-after.json、backups/ 和 logs-before/。运行数据与原始日志不纳入源码提交。

## 明确限制

HTTP 范围检查不证明自然语言工作范围完整，也不理解响应正文或任意漏洞语义；原始输出仍在工件。模型可自主选择行动，程序不规定下一工具。不新增总时长/费用上限、Planner、依赖任务图或验证 Agent。

身份和 authentication 按实际派发记录匹配，不独立证明服务器端权限或 Cookie 会话是否匿名；这些语义仍需模型解释或适用的独立验证。旧来源缺少认证或映射历史时不会补造记录。

本轮不恢复、重测或修改已有 Tomcat 任务的计划；旧完成 check 1 在实际继续后的结束检查中需引用已有来源补充范围。部署验收只核对入口、版本、同步及状态保留，不代替 P2。

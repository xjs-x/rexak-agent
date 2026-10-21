# 第一轮：证据驱动动态规划闭环工程记录

日期：2026-10-21。基线为 `6202b6b`，本轮修改在工作区中。行为约定见 [decision 0012](../../../decisions/0012-incremental-planning.md)。

## 实际实现

- 保留官方 `create_agent`、自主工具选择及 Runner/Executor 边界，无新增 Planner、Agent、DAG 调度器或任务表。
- Assessment 绑定更新前的步骤与版本；下一焦点独立保存，旧任务结果不会因切换焦点而被改挂到新任务。
- 调查型与达成型完成分开。检查区分模型语义判断、回执标量谓词、已有程序验证；检查记录仅由程序生成，来源完整不等于漏洞确认。
- 校验来源归属、工件哈希、操作状态与已有步骤关联。跨任务复用显式标记 supporting，保留原始归属。条件、指令、身份、来源及阶段变化后复查，不自动重新执行工具。
- 同批多个计划写入全部拒绝；过期版本拒绝；未完成事项不能直接删除；失效完成不能绕过复查直接归档。
- 稳定修订身份与不可变工件链保存完整变更。checkpoint 仍为唯一计划权威；Runner 使用官方同步 durability，从已提交状态补投影幂等事件。工件或流式 updates 不冒充提交回执。
- WorkingContext 呈现完成条件、关键变化和条件缺口；Progress 不因改写判断、换焦点或重排计划而重置事实进展。提示模型在关键结果后继续实际行动，不规定下一工具。
- API、生成类型、计划视图、报告和私有实验包接入完成范围、评估对象与修订来源。修订工件标记为候选来源，是否提交仍由 checkpoint 引用判定，不作为目标证据。
- State schema 为 6；旧完成标记待复查，不补造模式、依赖、验证或历史判断对象。迁移保留消息、操作身份和待执行节点，不自动启动历史终态任务。

## 推进协议验证

测试 `test_incremental_planning.py` 使用官方 Agent、生产 middleware 装配、Runner 输入/流式路径、真实隔离 PostgreSQL 和本地工具。脚本模型读取实际回执并生成来源引用；它依然是预设决策的协议替身。

| 场景 | 实际检查 |
| --- | --- |
| 前提失效后修复 | A 得到配置缺失结果；保留该结果，B 受阻，新增 C；C 实际取得替代条件后恢复 B 并执行最终检查 |
| 成功结果后展开 | A 取得条件后新增 C，并实际执行 C，再执行 B；不以计划文本代替下一行动 |
| 来源与执行关联 | 两条路径均实际执行三次工具行动，归属依次为 A、C、B；最终计划保留 A、B、C，修订为 1–4 |
| 任务状态 | 调查阴性允许结束调查，达成阴性和不确定结果拒绝 done；来源错配、缺失、未知和未同步不假报完成 |
| 修订一致性 | 无实质变化不加版本；已提交调用重放不增修订；同批竞争与旧头冲突拒绝；旧提议不能完成新计划 |
| 投影失败 | 候选工件未提交时不产生有效修订；事件插入后事务回滚，再并发补投影只写一次 |
| 恢复 | 逐工具边界关闭连接并重建官方图；独立子进程读取已提交新状态并补齐事件，操作记录仍只有一次 |
| 兼容与交付 | v1–v5 迁移保留 pending 节点；历史 done 不伪造验证；私有实验包包含可校验的修订工件与 checkpoint 引用 |

上述断言是协议判据，可对应结果后行动、状态正确性、无效重复、局部重规划与恢复一致性。它们不是模型自主推进率或真实目标成功率。

## 工程检查

最终命令：

```powershell
uv sync --frozen --all-packages --group dev
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration tests/recovery -q -p no:cacheprovider -m "not real_model and not real_executor"
uv run python scripts/export_openapi.py web/openapi.json
pnpm --dir web generate:api
pnpm --dir web typecheck
pnpm --dir web build
git diff --check
```

最终检查结果：

| 检查 | 结果 |
| --- | --- |
| 冻结依赖同步 | 通过，检查 91 个包 |
| Ruff / Pyright | 通过；类型检查 0 errors、0 warnings |
| 编码 | 通过，514 个文件为 UTF-8 无 BOM，无检测到的乱码 |
| 单元、集成及恢复回归 | 706 passed、2 skipped、1 deselected，耗时 216.49 秒 |
| OpenAPI 与前端类型生成 | 完成 |
| 前端类型检查及构建 | 通过 |
| diff 空白检查 | 通过 |

两项跳过来自 Windows 符号链接权限限制；真实模型测试按用户要求排除。回归出现 28 条既有 Pydantic 测试替身序列化警告，未影响断言。数据库测试使用独立临时库，不迁移日常工作库。

原始 pytest JUnit 位于实际数据根 `C:\xjs\rexak-agent\data\runtime-precontract-20261021` 下的 `acceptance/2026-10-21/incremental-planning/controller-tests.xml`，不纳入源码提交。

## 验证范围

本轮是源码与工程验证。没有使用真实 API Key 调用供应商，没有执行真实靶场、Executor 工具行动或效果实验；真实模型决策质量由用户另行验证。脚本模型按预设路径执行，不能据此宣称模型已经学会自主规划。

首次正常恢复时才进行支持的状态迁移。未新增第二轮 depends_on、任务总时长或费用硬上限。

## 补充：原桌面入口与 Kali 同步验收

用户明确要求今后每次代码交付默认包含本节验收，不以再次提出“验收落地”为条件。该约定已写入仓库 AGENTS.md。

- 更新前没有 API/Runner 进程，活动租约为 0；已有 1 个会话、1 个取消任务、8 条模型调用。数据库及工件完成备份，80 个文件校验通过。
- 从原桌面 `Rexak Agent.lnk` 实际启动，目标仍为本仓库 `start-workbench.cmd`。本轮新进程正常完成数据库、SSH、Executor 检查，API 和 Runner 均就绪。
- 运行 API 的 OpenAPI 实际包含新版 Completion、Assessment 与计划修订字段；控制台返回的 JS/CSS 哈希与本次 `web/dist` 构建一致。不是仅检查快捷方式路径。
- Kali 已安装的 23 个 Python 源文件及 `executor-control.py` 与本机源码哈希一致，身份、协议版本 1、能力读取通过，活动操作为 0。执行端代码本轮未变，实际已同步，故保留现有启动实例，不重复安装或重启。
- 验收后正常退出本次启动的 API/Runner，恢复验收前未运行的状态。31 张业务表的行数及内容摘要逐表一致，原会话、任务状态、模型调用和历史记录未改变。没有新增模型调用或访问目标。

原始部署核对位于上述实际数据根的 `acceptance/2026-10-21/incremental-planning/deployment/`，包括备份、前后数据库摘要、`kali-source-check.json`、`launcher-api.json` 及 checkpoint/Store 保留核对。日常取消任务未恢复执行，旧 checkpoint 未进行惰性状态迁移。

# 第三版工程与部署验收

开始日期：2026-10-14；完成日期：2026-10-15。
仓库：`C:/xjs/rexak-agent`。用户实现基线：`5da9902`，分支 `codex/implementation-v1`。

本记录验收[第三版方案](../../../plans/implementation-v3-weakness-chains.md)的代码、框架接入和个人部署。
真实 S2-045、Tomcat8、Earth 的模型效果测试由用户执行，本轮没有调用真实模型或连接靶场。
本轮没有读取或复制原始诊断自由文本、凭据、Cookie、隐藏推理及靶场答案。

## 结论与边界

弱点记录、关系消费、清单与链条计划、结束复查和同会话条件召回已形成工程闭环。
验收不能证明模型一定识别出有价值的连接，也不能证明自然语言理由语义正确。
后续实验应检查弱点是否实际改变行动、结束时是否仍有可推进方向，以及条件变化后的召回是否有效。

官方 `create_agent` 仍为主循环；业务表、checkpoint 和 Executor 台账分别保存领域事实、图状态和真实操作。
没有新增逐轮评审模型，也没有新增任务总时长、费用或工具调用次数硬上限。

## 复核发现及补修

| 缺口 | 本轮修正 | 验收依据 |
|---|---|---|
| 按 created_at 排序却仅用 UUID 推进完整库存，跨事务时可能重复或漏行 | 统一 UUID keyset；展示分页和完整库存分开 | 405 个时间顺序与 UUID 相反的对象，严格检查数量及唯一性 |
| 已确认/排除线索再次进入开放工作库存 | 关闭状态退出未完成库存，结果弱点独立复查 | 已排除对象不重新阻止结束 |
| pending 计划、blocked 覆盖项未计入有效阻塞；Runner 最后检查未传计划 | 完整检查计划和覆盖；最终检查使用 checkpoint 计划 | 未完成步骤阻止结束，受阻覆盖不靠标签放行 |
| needs_condition/no_new_action 可以用标签覆盖现有下一动作和缺失条件 | 检查判断与当前条件、已有尝试依据的一致性 | 空缺失条件、探索条件、未清除动作和伪造操作引用均不能放行 |
| 任何缺条件都可能整体等待；等待计数仅取当前页 | required_conditions.external 默认 false；明确外部依赖才等待；统计全量 | 可推进路线继续；7 条外部受阻路线均计入等待 |
| 弱点用途判断只检查线索存在，未检查关系 | 要求真实、未反证的 basis 关系 | 同会话无关线索不能冒充已建立连接 |
| 条件证据变化不推进内容版本；引用中的条件可删除 | 纳入证据、适用对象等内容；被引用条件禁止消失 | 条件证据修订与删除回归 |
| 乐观锁先读后写仍可并发覆盖；关系没有数据库同会话约束 | 短事务同会话锁；新增前向约束迁移 | 同版本并发仅一项成功；直接跨会话 SQL 写入失败 |
| 已暂缓旧线索主要依赖显式关系；同身份认证修订不使判断过期 | 增加资产/身份/关键词候选、认证修订与后台结果触发 | 未关联线索召回、同身份 auth_revision、后台结果及去重回归 |
| 召回去重存了字典文本；未发送对象推进版本游标 | 写真实 dedupe_key；提交只包含实际发送对象，保留此前已发送版本 | 呈现、裁剪、旧版本累计回归 |
| 固定扫描旧弱点前缀，后来的记录可能长期缺席 | 候选扫描覆盖会话；提示正文仍有界；已有效评审旧项不压住新项 | 485 条已评审旧记录之后的新弱点进入请求 |
| 多线索步骤的执行来源只覆盖主线，展示也只画 lead_id | 全步骤来源绑定；逐关系保留引用版本与当前版本；多线索页面 | 两条线索分别引用同一弱点的 v1/v2，执行记录不改写旧引用 |
| 新字段未完整纳入私有实验包；报告可能重报合并别名和过期判断 | 补 checkpoint 导出字段、元数据事件与报告时效说明 | 包、报告、官方图消费闭环及读取 API 回归 |
| 旧 record_lead 调用因新增默认字段可能改变请求摘要 | 无新参数时保留 V2 摘要口径及原回执 | 合成旧回执重放，没有新建领域记录 |

用途判断和可推进性判断仍由主模型完成。程序检查引用、版本、遗漏和结构矛盾，不用更多必填文字冒充语义验证。

## 框架闭环与兼容

官方图的合成模型测试覆盖：工具结果 → record_weakness → 下一请求的实际弱点内容 →
record_lead 建立关系 → update_plan → 合成操作派发 → 结果及阶段报告。
同时检查请求 manifest、checkpoint、业务关系和派发 execution_context，另测模型一直不消费弱点时不能自主结束。

保留 `propose_finding` 和旧 `record_lead` 的重放语义。
状态 schema 保持第三版的 v4，已有 1→2→3→4 迁移测试验证消息 ID、待执行节点和暂停恢复。
响应恢复包装顺序仍为 `ResponseRecovery → ContextBuilder → ModelObserver`。
系统提示版本为 6，方法资料集升级为 4，记录新增的外部条件和已有尝试规则。

`provided_conditions=[]` 显式清空，省略则保留；被关系或前置条件引用的条目必须先处理引用。
验证方法相同、结论独立的线索可共用步骤；同名条件的身份/来源/成立状态冲突时拒绝合并丢弃。

## 工程检查

最终完整结果见 [local-tests.xml](local-tests.xml) 与 [验收元数据](acceptance-evidence.json)。
完整回归 **542 passed、40 skipped**，另有 1 项真实模型用例主动排除；跳过及未运行项不计通过。
补强后的官方图闭环专项 **3 passed**，进一步核对操作台账而非仅核对内存派发上下文，见 [闭环结果](official-loop-tests.xml)。
静态检查、类型检查、UTF-8 无 BOM 编码检查、OpenAPI 生成和前端生产构建均通过。
测试使用随机隔离 PostgreSQL 和临时工件目录，不访问工作库作为测试夹具。

```powershell
uv sync --frozen --all-packages --group dev
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration tests/recovery executor/tests -q -m "not real_model and not real_executor"
uv run python scripts/export_openapi.py web/openapi.json
pnpm --dir web generate:api
pnpm --dir web build
```

Windows 专项跳过项不能算通过；Linux 进程、文件、结构化 HTTP、Cookie、TLS 和命令边界另在 Kali 隔离测试目录验证，
结果为 **26 passed**，见 [Kali 专项输出](kali-tests.log)。测试仅使用临时文件、进程和回环回显服务。
有 1 项第三方弃用警告，没有测试失败。

远端专项首次暴露测试夹具会删除原有 EXECUTOR_TOKEN 环境值，影响同进程后续测试；
已改为 pytest monkeypatch 自动恢复环境。TLS 夹具采用一次性 OpenSSL 证书，未向生产运行时安装额外测试依赖。

## 个人部署

- 工作库与工件已备份到本机 `data/backups/v3-acceptance/20261014T155249940797Z`，2283 个文件校验通过。
  备份保留在本机，不提交其正文。
- 工作库已升级到 `c9e40b17fa32`，两个关系同会话约束存在。原已应用的 `b3f1c7d2e904` 迁移没有改写。
- 更新前检查没有有效控制端运行租约，执行端没有活动任务。历史状态和未知操作未清理，也未自动恢复任务。
- Kali 已离线重装执行端 wheel，15 个上传文件及 11 个已安装源码文件哈希通过，pip check 通过；
  重启后的 boot_id 与原实例不同，身份、协议 1、四种请求体模式核对通过。
  见 [部署证据](deployment-evidence.json)。执行端生产代码本轮没有功能修改，仍同步重装并核对当前版本。
- 原桌面 `Rexak Agent.lnk` 指向本仓库 `start-workbench.cmd`。启动文件支持参数透传，原默认双击流程保留。
  通过原 CMD 入口的 API-only 检查确认控制台、OpenAPI、前端资源均为 HTTP 200，包含弱点/关系接口及多线索展示。
  见 [启动证据](launcher-evidence.json)。检查后启动的进程干净退出。
- 本轮没有启动真实 Runner 去消费工作库任务，也没有输入真实模型 Key。完整模型启动分支由合成启动回归覆盖；
  用户之后双击原入口，选择小米或百炼并输入对应 Key，进行自己的真实测试。

默认启动不需要重建前端或手动更新 Kali。控制台地址为 `http://127.0.0.1:8770/console/`。

## 用户真实实验的复核重点

固定代码提交、模型与参数、系统提示/方法资料版本和目标环境版本。
重点追踪四个不同事件：弱点已保存、实际呈现、模型判断、相关执行。
新版本增加 weakness.changed/presented/reviewed、relation.changed、condition.review_due 和 completion.review 元数据事件，
结合操作来源、请求 manifest 和阶段报告判断是否真正消费，不以关系数或请求数替代实际效果。

Earth 类样本检查原本孤立的弱点是否改变后续路线；Tomcat8 类样本检查未申报或暂缓线索是否仍被复查；
S2-045 及其他样本检查条件判断、已有尝试依据与结束理由是否相符。本记录不提供目标答案或测试路径。

原 `.workbuddy` 修改与 `docs/experiments/002-v1-three-targets-2026-10-13/` 保持独立，不纳入验收补修提交。

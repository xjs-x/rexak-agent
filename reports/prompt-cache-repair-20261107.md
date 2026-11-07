# Prompt Cache 最小修复与交接验收

日期：2026-11-07。保留工具效率工作及其 decision 0025；未提交、未推送 Git。

## 修改

- `agent/middleware/context_builder.py`、新增 `context_updates.py`：System 保留固定规则，计划、焦点、WorkingContext、Scope、技能资料和图维护动态数据改为独立上下文快照。只有内容变化时追加；同类最新完整快照整体替代旧快照，空值/缺失表示旧值失效。快照通过官方 `create_agent` 模型节点和原消息 reducer 随响应提交 Checkpoint；不增加主循环、状态表或缓存框架。失败/取消不提交响应或新快照。
- `agent/middleware/history_assistance.py`：保留原历史检索、范围、预算和控制校验，改为独立参考消息；不再临时修改旧 ToolMessage。成功发送的参考保留在 Checkpoint，已有且未变化的参考可直接复用。预算溢出时原有归档/压缩机制仍生效，相关来源保留。
- `agent/factory.py`：运行指纹包含新增消息助手及固定上下文规则，便于核验进程实际加载版本。
- `worker/src/model-provider.ts`、`pi-runtime.ts`、`contracts/delegation.py`、`observability/costs.py`：Worker 输入改为供应商含缓存总量；保留缓存字段缺失为未知，补充供应商别名。新增输入口径标记，旧结果默认为 Pi 原来的非缓存口径，读取时转换，原工件不改写。
- 新增 `domain/worker_usage.py`；`api/routes.py`、`contracts/usage.py`：从既有 Worker 结果工件读取统计，校验会话、任务身份、长度和哈希；Main、Worker 输入及缓存读取分项返回。运行中/结果未同步的 Worker 明确不计入已知小计，不增加第二份用量台账。原顶部费用/总量仍是 Main，文案明确标注。
- `web/src/api/client.ts`、`console/TaskDrawer.tsx`、生成的 `schema.d.ts`：使用协议生成类型并展示分项；修复交接报告指出的 unknown→ReactNode 构建失败。保留 PROMPT_VERSION=20 的路径/环境/诊断规则、工具 Schema 和 `execution_environment` 快照；不刷新已有 observed_at，不删除诊断字段。

## 专项验证

接续阶段没有重新运行全量测试；只执行受影响链路及失败用例的定向回归。

- 上下文、历史辅助、目标映射、Token 成本、请求预算、响应恢复：68 passed。
- 扩展的源码指纹、统计、图维护、Decision Memory、技能专项共58例：50例先通过；8例原来假定动态资料位于 System 的断言改到上下文消息后定向通过（2 + 6）。保留图工具权限、分页、分段、Checkpoint 恢复及自然结束的验证。
- Worker 模型统计、原生循环、历史脱敏专项：18 passed / 1 skipped。跳过项依赖独立 Worker 测试数据库；没有作为通过计入。
- 本机服务重启后的最终缓存专项：`tests/unit/test_prompt_cache.py`、`tests/integration/test_prompt_cache_usage.py`，6 passed。覆盖固定 System、跨运行恢复、旧请求前缀不变、最新状态替代、环境观察时间保留、相同快照去重、A→B→A 不覆盖旧ID、失败保留原历史、旧/新 Worker 口径、会话隔离与工件损坏。
- 修改范围 Ruff、定向 Pyright（0 errors）、编码检查（774文件）和 `git diff --check` 通过；控制端依赖同步、前端依赖核对、前端构建和 Worker 构建通过。
- 仓库全量 Ruff 仍有57项既有实验/历史目录问题，未扩大范围修改。测试中的 SimpleNamespace/Toolbox 序列化警告不代表真实请求验证。

## 桌面与 Kali 验收

- 原 `Rexak Agent.lnk` 仍指向 `C:\xjs\rexak-agent\start-workbench.cmd`。通过该入口使用 `--no-recover --no-browser` 启动最终 API、Worker、Runner，保留历史恢复状态；没有让旧 recovering Run 自动进行真实模型实验。
- `/health`、`/console/`、`/openapi.json`、既有 Session 的在线 `/usage` 均为200。API 已加载代码指纹与当前代码一致，新统计协议已加载，控制台HTML与最终前端构建一致；在线统计包含 main/worker。
- Worker 在最终构建后启动，服务身份为 Pi / A2A 1.0，保留原独立模型配置；未发起真实模型请求。
- Kali 26个已安装源码文件与本机哈希一致；执行端身份、协议及 `analysis_diagnostics` 能力通过，active_jobs=0。代码未变且哈希一致，保留原 boot_id 对应进程，未重复部署/重启。工具效率工作已有的 Kali 8项及6个合成场景验收见其交接报告，本次不重复。
- 前后9个历史非终态Run的状态摘要、`.env`摘要一致；Runs=81、Sessions=80、ModelCalls=1069、Artifacts=3103、AgentDelegations=7，均未变化。活动租约和 Worker 活动请求均为0。没有删除历史、修改凭据或发起靶场实验。
- 私有运行证据：用户目录 `.workbench/prompt-cache-before.json`、`prompt-cache-pre-restart.json`、`prompt-cache-after.json`、`prompt-cache-launch.log`。

## 遗留边界

真实供应商缓存命中率提升尚未验证，本次不能宣称1.9%已提高。上下文容量触发的压缩/归档仍可能改变前缀；Worker统计只包含已同步的结果，旧结果缺少字段时保留未知；Worker缓存参数透传与会话亲和策略未纳入此次三项修复。

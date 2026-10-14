# 启动模型选择与百炼接入

本次修改仅位于 `C:/xjs/rexak-agent`，原 `C:/xjs/xak-agent` 未修改。

## 使用方式

双击原桌面 **Rexak Agent** 入口，选择 `1` 小米 MiMo 或 `2` 阿里云百炼
`deepseek-v4.1-flash`，随后在启动窗口输入对应 API Key。输入不显示，每次启动重新输入。
菜单出现在依赖检查之前；空 Key 会中止启动，不启动 API 和 Runner。

用户提供的百炼北京业务空间 Base URL 已配置到本机、被 Git 忽略的 `.env`，
无需每天输入地址。本记录不复制业务空间地址或任何 Key。可用 `DASHSCOPE_BASE_URL`
配置其他业务空间地址；不能填完整 `/chat/completions` 路径。

选定模型应用于本次启动创建的新 run。暂停恢复的已有 run 保留原供应商、模型和
首次 Runner 装配时的非秘密配置快照；若需继续小米任务，应启动时选小米并输入小米 Key。
已结束任务的下一阶段按本次启动选择创建新的 run。

## 落地机制

- 保留官方 `create_agent`。供应商选择通过启动器子进程环境传递给 API 与 Runner；
  主调用、响应恢复与上下文摘要沿用同一个模型绑定和原用量台账。
- Key 仅传给 Runner。API、数据库检查、doctor 的两种模型 Key 环境值置空，
  显式覆盖 `.env`，防止子进程再次读入历史凭据。API 仅收到“本次 Key 已输入”的布尔标记，
  该标记不代表凭据已通过供应商认证。Key 不写入 argv、配置快照、普通日志或 Kali。
- 新增百炼档案及参数适配。抽出共有的 `reasoning_content` 收取与工具轮次回传实现，
  保留原小米适配入口。百炼显式使用 Chat Completions，开启流末 usage 请求；
  将 SDK 改名后的输出参数恢复为百炼文档中的 `max_tokens`。
- 修正原 Runner 的模型装配：实际模型以 run 字段和绑定快照为准，避免默认供应商
  与任务台账供应商不同。首次装配固定快照；已有旧 run 在首次新版装配时建立快照，
  不声称恢复了此前未保存的配置。
- 设置页面显示本次启动选择；模型设置协议与前端类型由 OpenAPI 生成。
  百炼上下文预算初值为本机保守的 131,072 tokens，可由 `DASHSCOPE_CONTEXT_LIMIT` 调整。
  这不是供应商最大上下文能力声明。输出仍沿用本项目的单次请求配置，不增设任务总费用或时限。

协议依据：[阿里云百炼 DeepSeek API 官方文档](https://help.aliyun.com/zh/model-studio/deepseek-api)。
文档列出业务空间与地域接入地址、`deepseek-v4.1-flash`、多轮对话与 Function Calling，
并说明该模型默认思考。模型上下文长度与价格需以控制台为准；本次没有编造价格或用
本地合成协议测试将供应商实际能力标成“已验证”，能力字段保留 `unknown`，费用保留未定价。

## 验证证据与限制

- `uv sync --frozen --all-packages --group dev`、`ruff check .`、`pyright` 通过。
- `tests/unit + tests/integration + tests/recovery`：462 passed、3 skipped。
  跳过为两项 Windows 符号链接权限测试与一项未提供真实小米 Key 的测试；
  12 项告警来自原有阶段报告测试的模拟 Toolbox 序列化，不是百炼接入告警。
- 最后一次菜单顺序调整后，选择、协议、绑定相关 26 项重新验证通过。
  两个供应商均覆盖实际 SDK 的合成 SSE 工具调用、思考字段回传、usage 与输出额度。
  完整启动器流程使用模拟服务子进程，核对 API/Runner 默认值一致、仅所选 Key 进入 Runner、
  检查进程无 Key、argv 和日志无 Key。真实入口脚本验证了两个菜单分支与空 Key 退出；
  隐藏输入在该自动化检查中替换为桩函数，没有输入真实凭据。
- 隔离 PostgreSQL 覆盖新聊天模型字段、设置接口、模型快照保存及配置变化后的恢复；
  原小米任务不会错误使用百炼 Key。所有数据库测试使用随机隔离库，未运行日常任务。
- OpenAPI 类型生成、前端类型检查及生产构建通过。直接启动真实 API 进程分别注入
  两个供应商默认值，设置接口、控制台与新 JS 工件均返回 HTTP 200；API 未持有模型 Key，
  未启动 Runner。检查进程已退出。
- 桌面 `Rexak Agent.lnk` 目标与工作目录仍为本仓库 `start-workbench.cmd` 和仓库根目录，
  双击链路直接读取新版启动器与已构建前端，不需要重新制作 EXE。
- 本次 Executor 源码与协议未修改。本次尝试通过配置中的 SSH 地址核对远端哈希和能力，
  连接超时；本机 VMware `vmrun list` 返回 0 个运行中的虚拟机。
  因此**本次 Kali 在线核对及完整双端启动未通过验收**，不能把旧部署结果当作本次在线结果。

未调用真实小米或百炼模型，未执行真实靶场测试。供应商 Key 的有效性、账户模型可用性、
真实推理质量和测试效果留给用户启动后的实验验证。

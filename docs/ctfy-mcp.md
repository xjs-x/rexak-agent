# CTFy MCP 接入

核对日期：2026-11-11。当前交付状态为 **项目 Python 客户端只读实连及控制端部署验收通过；完整参赛链路未验收**。
本项目 Python 客户端已真实完成 MCP 握手、工具发现与赛事查询；Main 工厂装配两个 CTFy 入口，原桌面入口以不恢复历史任务的方式启动。未调用模型或验证 Kali 题目链路。

## 配置和入口

继续使用原 `start-workbench.cmd` / 桌面 Rexak Agent 入口。运行设置：

```dotenv
WORKBENCH_CTFY_MCP_ENABLED=true
WORKBENCH_CTFY_MCP_URL=https://ctfy.tsinghua.edu.cn/api/v1/mcp/
WORKBENCH_CTFY_MCP_TIMEOUT_SECONDS=180
WORKBENCH_CTFY_MCP_CREDENTIALS_FILE=~/.workbench/ctfy/credentials.json
```

`CTFY_TOKEN` 环境变量优先，其次读取独立 JSON 凭据文件的 `token` 字段。
真实令牌不写入仓库、`.env`、模型提示或 Kali；本地 `.env` 只保存文件路径引用。
缺失凭据明确报错，不回退模型 Key。

本机凭据已迁至用户目录独立文件，ACL 仅允许 HP 与 SYSTEM；原临时副本保留。
没有宣称加密存储。迁至 VPS 时应采用 `CTFY_TOKEN` 运行环境注入，
或由部署账户在 `~/.workbench/ctfy/credentials.json` 创建权限 0600 的文件；
不要复制 Windows 临时目录路径。

启用后 Main 获得 `ctfy_list_tools` 和 `ctfy_call_tool` 两个入口。前者分页列出
服务端发现的全部工具，指定 `name` 可读真实 inputSchema；后者按真实参数调用。
不在 Rexak 代码中固定比赛或工具白名单；访问权限由平台令牌和平台服务端决定。
目前服务端发现 96 个工具，包括 `list_competitions`。调用有副作用的工具仍经既有
OperationRegistry 记录，失败结果不自动重试。

独立只读检查：

```powershell
.venv\Scripts\python.exe scripts/check_ctfy_mcp.py
```

该命令不调用模型，不启动实例，不提交答案；已真实查询 `list_competitions` 第一页。

## 运行边界

- 复用原官方 create_agent 和 ToolDispatch；MCP 工具只装配给 Main。
- URL 和认证头仅由运维配置，禁止 HTTP 明文、URL 内嵌凭据和认证请求重定向。
- HTTP 直连，不继承代理变量；证书校验保留。支持 JSON 与 SSE，单请求总超时 180 秒，响应上限 4 MiB。
- 平台 start_instance 会等待就绪，排队过长可能超时；不能把超时视为未创建。
  使用 list_instances / get_instance_status 核对，不自动重放 tools/call。
- 写操作走原 OperationRegistry，已保存回执在重放时复用；回执不确定时返回 unknown。
  当前重放前仍需完成 MCP 前置发现，断网时可能无法立即读取缓存回执，但不会重发写操作。
- 比赛和实例范围由平台令牌及服务端控制；平台响应不会自动扩大 Kali Run Scope。
  长响应落原 ArtifactStore 和业务元数据，仅摘要和 artifact_id 进入上下文。
- 平台实例地址不会自动扩大 Run Scope。需要另行完成题目范围绑定、附件自动导入、
  Kali VPN 路由和 HTTP/TCP/TLS-SNI 连接核验，才可进行端到端参赛验证。
- 比赛关闭或分段暂停，以平台返回为准；本次未实现整场无人值守调度器。

## 本轮复核（2026-11-11）

原桌面快捷方式仍指向本仓库 `start-workbench.cmd`。实际使用
`--api-only --no-recover --no-browser` 执行后，在进入项目启动逻辑前即遇到
`uv trampoline failed to spawn Python child process / permission denied (os error 5)`。
当前会话访问 Docker 的命名管道同样被拒绝，这不能解释为 Docker 或 Kali 未启动。
Kali SSH 在保留主机校验的情况下因私钥权限检查失败；没有修改权限或重启执行端。

本轮补修凭据文件解析错误脱敏、目录说明脱敏、SSE 严格 UTF-8 解码与行缓冲前字节限制、
会话清理总超时、JSON-RPC 错误码脱敏，以及平台启动实例 schema 变化时的赛道约束。
只读脚本增加开关和返回比赛 ID 检查。补充工具装配、SSE 分块/非法字节/超限、超时与取消、
写操作 unknown 和重放不派发等测试；**这些 Python 测试尚未执行**。

本客户端为基于 httpx 的有限实现，未使用官方 MCP SDK；不支持服务端 sampling、roots、
elicitation、SSE 断线续传或完整双向请求，不能视为完整 MCP 兼容验证。
定向 Ruff 与 Node 严格编码/令牌排除检查通过；原 Python 编码检查、Python 客户端实连、
Main 运行态装配及 Kali 安装哈希对比仍未通过验收。详细执行结果见交付记录。

## 新会话验收结果

此前的权限阻碍已变化。项目 Python 只读实连成功；原 Python 编码检查、25 项新增测试、前端类型检查及构建通过。用户确认无活动任务后，原入口以 `--no-recover --no-browser` 启动 API/Runner；项目工厂创建的 AgentRuntime manifest 实际包含两个 CTFy 入口。Kali Executor 的身份、协议、活动作业及 27 个已安装 Python 文件哈希核对通过，执行端进程保留。完整回归为 1283 通过、2 个既有失败、5 跳过；Pyright 全树仍有 9 个其他测试文件类型错误。模型调用、真实题目及完整自主参赛均未验收。详见[交付记录](../reports/ctfy-mcp-setup-20261111.md)。

凭据随后已复制至 `C:\Users\HP\.workbench\ctfy\credentials.json`，仅 HP 与 SYSTEM 可访问；本机 `.env` 已指向新位置，原临时副本保留。新路径下项目 Python 只读实连再次通过，并以原入口 `--no-recover --no-browser` 重新启动，使运行进程读取新配置。模型调用、真实题目及完整自主参赛仍未验收。

## 官方依据

- [当前平台接入文档](https://ctfy.tsinghua.edu.cn/docs)
- [CTFY-0001](https://ctfy.tsinghua.edu.cn/docs/standards/CTFY-0001)
- [智能体团队赛道](https://ctfy.tsinghua.edu.cn/competitions/c0d8b4e3dd3b42c095d63d7dfa66431f)
- [赛事说明及 v2.5 规则手册](https://ctfy.tsinghua.edu.cn/competitions/c0d8b4e3dd3b42c095d63d7dfa66431f/rules)

本届规则限制解题 Agent 公网访问，要求工具预装；模型需经允许网关，正式赛模型规则
另有限制。MCP 接通不改变这些比赛规则，也不证明当前模型端点已符合正式比赛要求。

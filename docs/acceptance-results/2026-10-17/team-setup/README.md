# 新成员部署整理验证

基线：`61f95f7c9336337c132c9b5ec02793b5732914d6`，分支 `codex/implementation-v1`。开工工作区干净。本次只整理团队部署配置、初始化脚本、入口及文档，不更改 Agent 主循环或执行协议。

## 实现

- `.env.example`移除维护者 IP 和固定源码目录；控制端默认数据/备份路径跟随仓库。
- `setup_workbench.py configure`独占新建 `.env`并生成三个独立令牌，已有文件拒绝覆盖。
- `setup_workbench.py install`串联数据库、前端、wheel、Kali 首次 venv 和既有启动器检查；不启动 Runner。
- `deploy_executor.py --bootstrap`可创建缺失的专用 venv，检查 Python/Chromium，保留半成品或冲突配置，不自动删目录。首次安装不调用尚未安装的包来停止空服务；通过远端端口预检区分未监听和 HTTP 异常，不吞已监听服务的协议错误。
- 远端安装/数据路径按 SSH 登录用户解析，可配置远端 Python 可执行文件。原 `start-workbench.cmd`入口保持，已有个人 `.env`不改写。
- README、日常部署文档及新成员指南统一指向同一套步骤。
- 可通过个人配置选择公开 HTTPS Python 包源，不含凭据或查询参数；只影响本次命令，不改 Kali 全局 pip 配置，版本与哈希仍由锁定清单控制。

## 已验证

最终命令和计数见 [checks.json](checks.json)。配置 CLI 已在独立临时仓库目录创建配置，第二次运行拒绝覆盖；在现有工作区调用 configure 也按预期拒绝，调用前后个人 `.env`哈希一致。未打印或提交任何生成的令牌。

定向测试覆盖独立令牌、空白/中文路径、非法配置输入、远端 home 展开与越界拒绝、缺失连接信息、半成品 venv 保留，以及安装失败后不继续部署。另验证了 Windows 安装步骤执行器调用实际 pnpm 完成锁定安装和前端构建；Executor wheel 构建通过。

## Kali 实机复验

首次尝试因 SSH 超时未运行；用户启动 Kali 后，在独立安装/数据目录、随机端口和专属 PostgreSQL 测试库执行了完整 install。不是重装日常服务，也不是新建操作系统虚拟机。结果见 [kali-onboarding.json](kali-onboarding.json)：

- 首次从空目录创建 venv，安装锁定依赖和 wheel，28 个上传文件及 23 个安装源码哈希核对通过，trusted 能力与启动器 `--check-only`通过。
- 再次 bootstrap 部署复用 venv，远端 `.env`逐字节保持，启动实例更新，原进程启动身份核对有效。
- 控制端个人 `.env`哈希、日常 Executor boot ID 均保持，未启动 Runner、模型或测试目标。
- 专属 fixture 服务、安装/数据目录及测试库已清理；前两次未启动的临时环境也在核对命名空间、配置身份、无 PID/台账/进程后清理，见 [cleanup.json](cleanup.json)。

联调暴露并修正了新服务未监听时的 SSH 隧道错误分类：原实现只捕获 ConnectError，实机会返回 RemoteProtocolError。原失败见 [initial-first-install-failure.txt](initial-first-install-failure.txt)，新增回归验证未监听可继续，以及已监听但身份不符、存在活动任务或 HTTP 异常必须中止。默认 PyPI 下载过慢的 fixture 安装由验收者核对身份后中断，随后以公开镜像完成首次及重复安装；没有改变全局 pip 配置。

成功批次的顶层安装日志、首次及重复部署日志保存在本目录；`source-sha256.json`记录测试工作树的相关源码，报告不自引用最终提交 SHA。原桌面 cmd 未改动，本轮实际执行了其 Python 启动器的连接/数据库/能力检查，没有额外启动日常 API、Runner 或浏览器。

## 边界

完整脚本会依赖成员已配置好的 SSH 身份及主机指纹，并要求 Kali 有兼容 Python、venv、Chromium 和包源连接。它不会安装系统软件、处理 SSH 密码登录、复制用户虚拟机或提供全栈容器化。真实模型、真实靶场均未运行。

本次改动尚未推送到 GitHub；新成员需获取包含这些改动的团队分支/提交后才能使用新入口。

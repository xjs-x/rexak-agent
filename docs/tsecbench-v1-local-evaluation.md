# Tsecbench v1 本地评测接入

核对日期：2026-11-06。本文为接入准备，不表示已完成真实平台测评。

## 题集与推荐方式

用户选定截图为 Tsecbench v1，v1.0.0，63 题 / 74 个 flag，单次限时 6 小时。
实际题集及当前限制仍需由新任务的控制台和题目列表核对。
历史接入报告中的 104 题来自另一任务，不能当作本题集清单。

推荐先使用 **本地运行 + Python SDK**：Windows 保留控制端、数据库和 Ultra Worker，
Kali 保留 Executor；评测适配器通过现有 API 创建独立 Session/Run，继续复用原 Agent 和 Runner。
SDK 当前 PyPI 版本为 0.1.2，项目可选依赖范围兼容该版本。

| 方式 | 对当前项目的适用性 |
| --- | --- |
| 提示词接入 | 适合临时人工试用；本项目已有 SDK 编排，无需把平台令牌交给模型管理流程 |
| Python SDK | 推荐；已有单题适配器，可分别选择 Standard / Ultra，平台异常和结果可独立记录 |
| 直接 API | 可以实现，但当前没有必要再维护一套请求、解析和错误映射 |

本地与托管是运行模式，提示词 / SDK / API 是接入方式，两者不能混为一谈。
当前推荐不需要把整份源码上传到平台。
Git 提交用于固定评测版本，与上传平台 Docker 镜像是两件事。

托管模式留作下一阶段：官方准备页要求先完成本地接入；托管提供 Linux 沙箱，
8 核 / 16G、20G 总存储，运行时无公网，模型需要通过平台白名单网关访问。
现有 Windows + Kali 分离部署不能原样当作独立托管镜像上传；需要另行准备 Linux 运行环境、
数据库、Worker、执行工具、启动入口和离线依赖，并确认模型端点是否在允许范围。
官网当前说明公开 Agent 榜单来源为托管模式公开记录，本地评测不能直接等同于正式上榜。

## 已有流程与本次补齐

现有 `workbench tsec` 已实现：配置检查、取题、启动单题容器、构建题目授权范围、
从 Kali 检查连通性、创建独立 Run、等待最终回答、提取候选 flag、提交与关闭容器。
运行记录写入运行数据目录，评分以平台响应为准。

本次增加 `run --execution-mode standard|ultra`，使用协议真源的 ExecutionMode 枚举，
并将选择传给现有 Session 创建 API、写入评测记录。默认仍为 Standard，
评估当前 Ultra 配置时必须显式传 `--execution-mode ultra`。
本次离线检查不等于 Ultra 已在平台完成取题、解题和提交。

命令行 `workbench tsec run` 仍为单题；新增的桌面评测启动器提供单题和全部未完成题目串行运行。
整场截止时间由平台管理，启动器不延长有效期，也不自动恢复中断的批次。
首次使用仍建议先完成单题端到端验证，再安排整套题集。
现有适配器默认每题 3600 秒是旧入口的本地参数，不是对本题集每题时限的核定；
正式运行前应按本轮平台规则和整场 6 小时安排设置，不能按 63 × 1 小时估算本场时间。
现有候选提取只接受最终回答中的 `flag{...}`，在任务运行期间不即时提交工具输出；
若实际题目要求其他答案格式，需先扩展适配器。
现有流程未自动调用扣分提示接口。

## 凭据到位后的顺序

1. 在平台确认 Tsecbench v1、Agent 本地模式及 Agent 标识，获取这一轮的配置。
2. 将 `BENCHMARK_TOKEN`、`BENCHMARK_BASE_URL`、VPN 配置放在仓库外的
   `C:\Users\HP\.workbench\tsecbench\`；保留旧配置，不覆盖历史凭据。
   同一 VPN 配置能否被两端同时使用，必须实测，不能默认支持。
3. Windows 与 Kali 分别验证 VPN 连通性。Windows VPN 不代表 Kali 自动具备路由。
   SDK 默认 VPN 预检地址为 `http://10.0.100.58`；Kali 还需验证实际题目地址。
   若平台限制同一配置同时在线，需要确定单一 VPN 出口或获取适用的双端配置。
4. 用已有桌面入口启动本地服务，并保留旧任务恢复状态。只读核对平台题目数量、
   flag 总数、任务有效期及范围，不先扫描靶场。
5. 先选一道题预演，再由用户启动真实单题端到端检查；核对平台提交回执、
   多 flag 进度、容器释放和 Run 记录后，再安排整套评测。

常用命令（均在项目目录执行；预演和取题仍需要有效平台配置）：

```powershell
uv run --extra benchmark workbench tsec doctor
uv run --extra benchmark workbench tsec list --all --json
uv run --extra benchmark workbench tsec run <unique_code> --execution-mode ultra --dry-run
uv run --extra benchmark workbench tsec run <unique_code> --execution-mode ultra
```

平台 Token 只供评测适配器使用；模型密钥继续由现有本机模型设置管理，不下发 Kali。
VPN 文件可能包含私钥，不提交 Git，不写入公开报告或镜像。

## 2026-11-06 网络实测补充

切换手机热点后，Windows OpenVPN 自动完成握手；Kali 不建立独立 tun 接口，
通过 VMware NAT 和 Windows VPN 出口即可直连健康地址。两端健康检查通过，
取题仍为 63 题 / 74 个 flag、全部未作答。此证据仅覆盖健康地址和取题，
实际题目地址仍由启动后的 Kali 目标探针核验，不能据此宣称所有靶场可达。

另发现 Windows 系统代理会使 SDK 的 VPN 健康请求返回 502。
适配器现在显式传入不读取代理环境的 HTTP transport，VPN 和平台请求均直连，
不修改 Windows 全局代理。回归用本地 HTTP 服务和不可达代理验证这条边界。

按用户要求，Kali 普通用户和 root 的 shell 启动文件会加载
`/etc/profile.d/rexak-direct-network.sh`：清除 HTTP / HTTPS / ALL / FTP 代理变量，
并设置 `NO_PROXY=*`、`no_proxy=*`。原启动文件均已备份。
该设置对新终端自动生效；已打开的终端执行以下命令立即加载：

```bash
source /etc/profile.d/rexak-direct-network.sh
```

直连设置不改变 Linux 默认路由，也不重启 Executor；现有进程的环境不会被 shell 文件追溯修改。

## 官方来源

- [平台及三种接入入口](https://tsecbench.zc.tencent.com/)
- [本题集本地准备页](https://tsecbench.zc.tencent.com/benchmark/prepare/4?mode=local&evaluation_type=agent)
- [官方页面加载的接入说明](https://tsecbench.zc.tencent.com/assets/IntegrationGuide-ci2oGUk6.js)
- [官方页面加载的本地与托管说明](https://tsecbench.zc.tencent.com/assets/BenchmarkPrepareView-DC2QLK2L.js)
- [Python SDK 发行说明](https://pypi.org/project/tsec-benchmark/)

页面资源名可能随官网更新变化；入口页面为后续复核起点。

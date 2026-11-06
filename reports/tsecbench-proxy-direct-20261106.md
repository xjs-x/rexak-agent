# TSecBench 直连与 VPN 网络验证

日期：2026-11-06。

## 结果

手机热点下 Windows OpenVPN 自动完成握手，VPN 健康地址可达。
Kali 通过 VMware NAT 复用 Windows VPN 出口，默认路由保持不变，
无独立 tun 接口也可直接访问健康地址。实际靶场访问尚未验证。
平台新任务读取成功：63 题、74 个 flag、0 个正确 flag；未启动靶场或模型任务。

原网络中两端均 TLS 握手超时，Kali 抓包只有出站 UDP，无入站响应；
热点切换后 Windows 自动恢复，因此原网络路径很可能是超时原因。
不能据此推断所有原网络防火墙规则或平台服务端配置。

## 代理问题与修复

发现 SDK 默认 httpx 客户端读取 Windows 系统代理，VPN 内网请求返回 502；
相同地址显式直连返回 200。环境中没有 HTTP_PROXY 变量，代理来自系统配置。

`SdkPlatformClient` 显式传入 `httpx.AsyncHTTPTransport(trust_env=False)`，
让 SDK 不再发现系统或环境代理；TLS 证书验证仍启用。
未改 Windows 代理软件设置，也未修改 SDK 包源码。

新增本地服务回归：先证明不可达代理导致默认客户端失败，再验证真实 SDK
在同一代理环境下能够完成 VPN 健康检查及题目列表读取。
首次测试对代理失败类别限定过窄（ConnectError），本机实际为 ConnectTimeout；
已改为验证传输失败，再确认直连成功，最终适配器专项 43 passed。
本机真实 SDK 预检和只读取题也通过。

## Kali 配置

检查时普通用户和 root 未发现有效代理环境或指向所述代理端口的 shell 配置，
普通用户桌面代理模式为 none。
按用户要求增加持久直连保障：

- `/etc/profile.d/rexak-direct-network.sh` 清除 HTTP/HTTPS/ALL/FTP 代理变量，设置 NO_PROXY/no_proxy 为 `*`。
- `/home/kali` 和 `/root` 下 `.bashrc`、`.zshrc`、`.profile` 加载该片段，六份原文件均已备份。
- 分别以两个用户注入用户所述代理，再加载片段，均验证代理变量清除、健康地址返回正常。

未改变默认路由、防火墙或系统服务。新终端自动加载；现有终端若需要立即加载，
执行 `source /etc/profile.d/rexak-direct-network.sh`。
没有将凭据写入源码、Git、直连配置片段或本报告。

## 验证记录

类型、产品代码 Ruff、编码检查通过。前端和 Worker 构建通过。
Kali 25 个已安装文件与本机源码一致、Executor 身份正确、活动 Job 为 0，保留现有进程。
完整 unit/integration（包含 benchmark extra）：1136 passed / 4 skipped / 46 warnings，476.37 秒。
四个跳过项为 Windows 符号链接权限两个用例、显式 Kali 附件验收和未配置密钥的真实模型检查。
原桌面入口 API-only 短暂启动通过，Ultra Worker/A2A 检查通过，启动器随后清理本次服务。
8 个未结束任务摘要、原本机配置摘要和 Executor 启动身份前后一致，未续跑旧任务。
本轮不将该 API-only 核验表述为真实模型运行或完整 Runner 运行验收。

全仓 Ruff 仍有历史实验目录问题，未修改历史实验数据。
本轮不代表完成单题解题、平台 flag 提交、题集自动调度或托管上榜。

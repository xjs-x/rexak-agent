# HTTP 未知送达与取消收尾修复

日期：2026-11-06。

## 本次现场处理

最新 e2-04 Run 的一个 HTTP 请求遇到 RemoteProtocolError。
Executor 已记录 finished_at、http_transport_error，未创建 PID；其业务送达状态为 unknown。
原取消 API 却调用仅用于子进程的 supervisor，因没有进程句柄无法确认，令 Run 长期 cancelling。

用户明确要求提交已有 flag、优先安全退出。仅从本 Run、本 Session 的工具输出工件中
提取候选，找到 1 个后提交本轮平台，回执 correct=true、progress=1/1。
原件、候选哈希和提交来源记录保存在运行数据目录，报告不包含完整答案或解题路径。

为立即退出，对唯一关联 HTTP Job 做了一次执行端台账恢复：校验 job/operation 身份、
HTTP kind、已持久化的 finished_at、传输异常、无 PID、已有取消请求，
在该 Job 目录保存 manual-cancel-confirmation.json，明确 target delivery 仍为 unknown。
仅记录此已结束请求的取消确认，保留原传输错误，没有标记为 succeeded。
随后原控制端取消链将 Run 转为 cancelled 并释放租约，平台确认成绩 1/1、容器 stopped。
窗口清单也确认启动器已退出。未重跑题目或调用新模型。

## 代码修复

- Executor：已终结任务取消直接确认退出并保留原状态；已结束的 HTTP 协程不查找子进程句柄。
  仍运行的 HTTP 协程执行 cancel 并等待完成；没有停止证据仍返回未确认。
  HTTP 传输完成与取消并发时重读台账，保留 unknown 送达结果。
- 控制端：只对 HTTP、且有 Executor finished_at 的 unknown 作业，区分“执行已结束”和
  “目标送达未知”；普通 unknown、命令 unknown、没有结束证据的 HTTP 仍阻止自动推进。
  轮询保存真实结束时间，唤醒通知明确禁止自动重放原请求，不把 unknown 变成成功。
- Job 读取 API 提供独立 execution_finished 标记；评测收尾据执行生命周期判断是否仍有
  待停止作业，原 state/sync_status 和送达未知提示保留。

## 验证与部署

Executor 7 项针对性回归通过，覆盖成功/失败/已取消/未知完成、重复取消、活跃协程取消、
无完成证据拒绝确认，以及并发完成保留未知。
控制端相关专项 50 项通过；补充的评测窗口场景与后台唤醒专项最终 35 项通过，数量不累加。
类型、编码、产品源码 Ruff、前端和 Worker 构建通过；全仓历史 experiments 目录仍有 57 项 Ruff 问题。

Kali 已安装新版 wheel，更新重启后验证 25 个文件哈希、启动实例身份和协议。
曾在 Kali 本机启动一次只断开连接的临时 HTTP 服务：真实 Executor 记录 unknown 且 finished_at
存在，连续两次取消均 process_confirmed_exited=true、state=unknown。没有访问真实靶场、没有模型调用。
随后补充并发保护由 7 项最终 Executor 单测覆盖，并再次构建、部署和核对新版实例。
部署前无活动运行租约，历史运行数据、凭据与暂停状态保留。

完整 unit/integration 回归：1160 passed、4 skipped、46 warnings（483.72 秒）。
跳过项为 Windows 符号链接权限、需显式启用的 Kali 附件验收和未配置的历史 Xiaomi 模型检查。
历史 8 个非终态 Run（5 paused、3 recovering）的快照摘要保持，最终 Executor active_jobs=0。
原桌面快捷方式仍指向本仓库 start-workbench.cmd；经该入口 API-only 烟测成功启动
控制台和 Ultra Worker / A2A，退出码 0，随后清理本次启动的服务，没有启动 Runner。
.env 哈希及上述历史 Run 快照摘要与修复前一致。
私有部署/保留证据位于用户目录 .workbench/tsecbench 的
http-cancel-deployment-final.json、http-cancel-preservation-final.json 和 http-cancel-desktop-smoke.log。
用户离开热点仅影响测评 VPN；修复和本地/Kali 管理验证使用普通网络及现有管理连接。

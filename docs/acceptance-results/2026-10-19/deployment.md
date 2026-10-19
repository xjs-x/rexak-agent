# 完整方案的桌面与 Kali 部署验收

2026-10-19。产品修复：普通 Runner checkpoint owner / lease 门禁、多组冻结 profile 报告显式选择。迁移结果见同目录 plan-rollout.md。

## 控制端

- frozen 依赖同步通过；全量集成 386 passed / 1 skipped、单元 331 passed / 2 skipped，独立审计 24、旧审计 13、主探针 8 通过。
- Ruff、Pyright、UTF-8 无 BOM 检查、前端 tsc 与 Vite build 通过。
- 保留原桌面 Rexak Agent 快捷方式及目标脚本，快捷方式哈希未变；从其解析出的原 start-workbench.cmd 启动 API-only 烟测。
- 实际监听端口的 health、console、plan 均 HTTP 200；服务的 HTML、JS、CSS 与本次 dist 文件逐字节一致，合同工作集和旧计划只读投影标识包含在实际服务的 bundle 中。
- 烟测后 API 正常停止、端口释放，未遗留 Runner；数据库与 Kali Executor 保留运行。未修改默认桌面入口行为。

桌面验收采用 API-only，避免自动消费历史任务，不代表已经执行真实 Agent 模型 / 靶场实验。真实会话恢复与效果测试由用户执行。

## Kali

- 使用现有 SSH / Executor 配置部署离线 wheel；28 个上传文件校验通过，23 个已安装源码哈希与当前源码一致。
- Executor 已重启，启动身份确实改变；health / capabilities 身份、协议版本、trusted 模式与 pip check 通过，active_jobs=0。
- 另在独立临时数据目录运行进程、文件、台账、终端和输出专项，54 passed。没有使用会重启测试目标的旧整包验收脚本，没有启动用户测试目标。
- 远端 .env 与部署前保留副本的配置值一致；本机 .env、模型配置 / 密钥文件、SSH 凭据文件哈希保持一致。

trusted 是当前受信执行配置，不表示具备系统级网络隔离。

## 保留状态

完整迁移和部署前后真实 model_calls 数量均为 826；运行状态计数一致，暂停的真实复盘会话与其两个未决操作未变。桌面烟测前后受保护表和相关 checkpoint 指纹一致；没有恢复会话或调用真实模型 / 目标。

本轮所有合格的 9 个旧会话已按受控批次迁移，仍有 14 个旧计划候选因删除、未决项或人工核对要求保留。没有为凑批次放宽门禁，也没有删除 append-only 合同。

逐会话、备份清单、进程 / 静态文件哈希、HTTP 返回、Linux JUnit 和保留状态证据位于本机 `data/acceptance/2026-10-19/full-rollout/`。该目录包含个人运行信息，不进入共享提交。

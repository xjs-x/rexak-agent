# Phineas checkpoint 写入冲突修复

## 真实记录与根因

只读核对最新 Phineas 会话：run 为 failed，stop_reason 与用户提供的 `uq_checkpoint_write_receipt` 错误一致；指定 checkpoint 的官方写入为 5 条，收据为 0。两个同通道的 Send 不能按通道名去重。

没有恢复、重跑或修改这个真实会话，没有删除 checkpoint，也没有为历史写入补造收据。

## 验证范围

隔离随机 PostgreSQL 数据库中，修复前的五条独立反例全部失败，日志保留在本机验收目录。新增覆盖：多 Send 并发幂等及重建包装器后恢复、位置异内容先拒绝、内层独立提交后的相同/不同请求重试、保留通道更新、收据事务失败、旧收据歧义、相同内容的不同位置不能共用收据、混合负索引与普通索引、暂停后拒写、真实 StateGraph 双 Send 分发和恢复。

最终全量单元及集成为 736 passed / 3 skipped；checkpoint 专项 22 passed，独立审计 24 passed，主探针 8 passed，旧审计已包含在全量集成中。Ruff、Pyright、UTF-8 无 BOM 检查通过。跳过项为 Windows 链接权限限制与未配置真实模型。

实际桌面服务通过原 `start-workbench.cmd` 启动链重启；原快捷方式未更改。验收核对健康接口、控制台与最新会话计划 API，启动前后 46 张受保护表及凭据哈希一致。真实模型调用数保持 834，失败会话及原暂停会话状态保留。

此修复只修改控制端，没有 Executor 源码变更。Kali 只读检查确认身份匹配、active_jobs=0；未进行真实模型或目标操作。

本机原始证据：`data/acceptance/2026-10-20/checkpoint-writes/`，包含 `diagnosis-before.json`、`reproduced.log`、`all-regression.log`、`targeted.log`、`independent-audit.log`、`main-probes.log`、`deployment-before.json`、`deployment-after.json`、静态检查日志和桌面启动日志。个人运行记录不提交。

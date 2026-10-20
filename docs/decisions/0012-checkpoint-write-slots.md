# 0012：checkpoint 收据按官方写入位置区分

2026-10-20。

## 问题

Phineas 最新运行在同一个 task 的一批 pending writes 中产生两个 `__pregel_tasks`。官方 AsyncPostgresSaver 以 `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` 区分它们；原包装层以通道名作为 write_key，导致收据唯一键冲突。业务数据库会话设置了 `autoflush=False`，逐行查询看不到本批尚未刷入数据库的收据，最终提交时整批回滚。

官方 Saver 使用独立连接，原始写入可能已提交。不能仅吞掉唯一键异常，或只保留两个任务中的一个。

## 决定

- 新收据沿用现有字段及索引，`write_key=v2:slot:<idx>`。普通写入使用整批位置，保留通道使用官方 `WRITES_IDX_MAP` 的负索引。
- 哈希绑定通道和框架类型序列化后的内容，包括 Send；不以字符串展示代替真实值。同一位置同内容幂等；普通位置异内容在调用内层 Saver 前拒绝。
- 保留通道批次遵循官方 UPSERT 语义；普通或混合批次遵循官方 INSERT 语义。不改变官方存储语句。
- 内层写入后，使用只读查询核对该 task 的实际位置、通道和内容，再提交收据。原始写入成功而收据失败时，仅实际内容一致的显式重试可补齐收据；不为未落库的请求内容签发收据。
- 恢复时按官方物理位置与收据核对，保留同通道的全部合法条目，不借用另一个位置的收据。没有收据或版本/哈希不符的条目保持不可消费。
- 旧通道键收据不改写、不推测位置。只有该 task/channel 唯一且原哈希、所有权版本一致时只读兼容；歧义情况拒绝恢复该条目。
- 原 writer epoch、owner generation、control version、有效租约和 accepted head 门禁保留；不重放真实操作，不清除失败历史。

无需数据库迁移。包装器固定与内层连接对应的 schema，故障注入或事务装饰层不需要额外暴露 settings。

验证记录见[验收](../acceptance-results/2026-10-20/checkpoint-writes.md)。

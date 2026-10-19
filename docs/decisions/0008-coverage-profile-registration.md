# 0008：冻结 coverage profile 的跨进程登记

日期：2026-10-19。状态：已实现并通过隔离 PostgreSQL 回归。

profile 定义以 `app.coverage_profiles` 为唯一权威。登记只能由显式 POST 发起；GET 和报告读取已登记定义，不隐式登记，不从 coverage 行推断另一套分母。本次不修改 API、报告接口或数据库结构。

## 原子幂等

`CoverageService.register_profile` 使用 PostgreSQL `INSERT … ON CONFLICT DO NOTHING RETURNING`，冲突键是现有的 `(session_id, profile_id, profile_version, condition_fingerprint)` 唯一约束。

插入成功返回新行。发生唯一键竞争时，在同一 READ COMMITTED 事务的下一条语句中重读已提交行并比较定义 hash；同定义返回同一 ID，不同定义返回 `coverage_profile_conflict`，HTTP 409。重读加行锁，串行化旧空 hash 的回填。

不使用进程内缓存或锁承担正确性，不使用 DO UPDATE 改写冻结定义，不捕获 IntegrityError 后回滚调用方整个事务再继续执行。

categories 顺序敏感，`include_parameter_cells` 和 `include_anonymous` 都纳入 hash。旧行 hash 为空时，先按已有行的 categories 和标志计算真实 hash；与请求相同才回填，不同则拒绝且不改旧行。事务内只做数据库操作。

## 可见性与验证

并行 GET 在首次登记事务提交前可以返回 409 `profile_not_registered`；提交后新请求读取到同一冻结定义。登记返回成功必须对应其外层事务提交成功，不能返回缓存中的未落库对象。

`tests/integration/test_profile_registration.py` 验证两个独立进程 / 事件循环 / 连接同时发起 HTTP POST：相同定义双 200、同 ID / hash；不同定义一 200、一 409。另覆盖回滚重试、提交前后 GET、旧空 hash 兼容、顺序和两个标志的冲突，以及调用方其他事务修改不被回滚。

并发测试屏障放在登记开始前，不能放在 INSERT 执行后：赢家持有唯一键等待屏障、输家等待赢家提交会制造测试自身的锁等待。

本轮通过 profile 10、独立审计 24、旧审计 13、主探针 8；全量集成 378 passed / 1 skipped，单元 331 passed / 2 skipped，Pyright 0 errors。跳过项不计为通过；这些结果不表示真实模型或目标测试效果。

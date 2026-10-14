# 编写本仓库代码的 Agent 约定
#
# 本文件说明"改这个仓库时必须遵守什么"，与 docs/implementation-status.md 配合阅读。

## 权威文档

- `docs/decisions/` —— 已锁定的技术决策与版本结论，冲突时以最新 decision 为准。
- `docs/implementation-status.md` —— P0–P9 逐项实现状态。**没有验证证据的能力不得标为完成。**

## 硬性约束

1. **不写第二套 Agent 主循环。** 基础模型—工具循环必须使用官方 `create_agent`，
   只在上下文、策略、结束判定处扩展 middleware。
2. **每项信息只有一个权威归属。** 图状态归 LangGraph checkpoint；领域记录归业务表；
   真实执行状态归 Executor 台账。不要新增"万能状态表"或可写的第二份记忆。
3. **模型猜测不得直接写成已确认事实。** 工具/模型只能写 candidate；`confirmed` 必须由
   验证记录支撑。
4. **不设任务总时长/费用硬上限。** 单次操作仍有超时、取消与资源边界。
5. **操作身份稳定。** 重放复用 `operation_id`；显式重测必须生成新的逻辑操作 ID。
   工具函数内随机生成 ID 会破坏幂等，属禁止行为。
6. **编码统一 UTF-8 无 BOM。** 提交前必须通过 `uv run python scripts/check_encoding.py`。
   未知外部字节保留原件 + 显式错误，禁止静默 GBK/Latin-1 兜底。
7. **不吞异常。** 取消、协议错误、超时、供应商错误分别处理；禁止 `except Exception: return {}`。
8. **长正文不塞 checkpoint / events / findings。** 落工件，只传摘要与 `artifact_id`。
9. **控制端不得让 Executor 持有模型 API Key 或数据库凭据。**
10. **未验证的能力在 UI/文档中必须显式标注为禁用或未实现**，不能使用"已支持"文案。

## 代码组织

- `src/agent_workbench/contracts/` 是协议真源；前端类型由 OpenAPI 生成，不手工维护第二份枚举。
- 依赖方向：`agent → tools → execution/domain`；`domain` 不 import `agent`；
  Executor 不 import 控制端仓储。
- 运行数据写在 `WORKBENCH_DATA_ROOT` 下，不放源码包。

## 个人部署验收约定

- 用户要求“验收落地”时，默认同时完成本机依赖/前端构建与 Kali Executor 更新、重启、文件哈希及能力核对，验证原桌面 **Rexak Agent** 入口可直接使用新版本。
- 原桌面入口指向本仓库 `start-workbench.cmd`，保持该双击流程。不要把 Kali 更新留作用户必做的收尾操作，也不要将源码更新等同于运行进程已加载新版。
- 更新前核对活动任务并保留凭据、运行数据和暂停恢复状态；遇到服务不可达或活动任务时如实记录未完成项。真实靶场及真实模型效果实验由用户执行，部署验收不自动发起实验。

## 提交前检查

```powershell
uv sync --frozen --all-packages --group dev
uv run ruff check .
uv run pyright
uv run python scripts/check_encoding.py
uv run pytest tests/unit tests/integration -q
```

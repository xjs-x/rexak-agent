# v1.0 公开验收摘要

本页汇总正式工程证据、历史 CTF 复盘和 README 展示素材，按验证范围分别记录。运行状态是验收时点的快照；源码提交不代表重新部署、创建 GitHub Release 或完成真实模型效果实验。v1.0 是本次工程交付里程碑，Python 与前端包元数据仍为 `0.1.0`。

## 2026-10-30 正式工程回归

原始工程结论见 [v1-finalization](../reports/v1-finalization.md)。该报告的日常部署状态记录于后续正式切换之前，应结合下节启动验收阅读。

| 检查 | 当时结果与范围 |
|---|---|
| `uv run pytest tests/unit tests/integration -q` | 1019 passed、3 skipped、0 failed，509.58 秒 |
| 跳过原因 | Windows 符号链接权限 2 项，未配置真实模型凭据 1 项 |
| `uv run ruff check src executor/src tests scripts evals` | 生产源码及相关检查范围通过 |
| `uv run pyright` | 0 errors、0 warnings |
| `uv run python scripts/check_encoding.py` | 660 个文件通过 UTF-8 无 BOM、无替换字符及已知乱码检查 |
| 前端 Vitest | 66 passed，6 个测试文件 |
| TypeScript / Vite | 构建通过，保留较大 chunk 警告 |

确定性跨模块测试覆盖 API → Runner → 官方 Agent → Operation → 工件 → Checkpoint 重读与最终答复归档，以及租约、控制版本、恢复、权限、附件、图来源和只读查询。它们证明工程协议，不是模型在未知靶场中的成功率证明。

同次额外检查的全仓 Ruff 在归档实验范围有 43 项问题；`tests/experiments` 为 120 passed、90 failed、6 skipped。它们没有合并进正式通过数，也没有通过改写冻结实验源码消除差异。

## 2026-10-30 日常 API、Runner 与历史读取

来源：维护验收报告 `reports/v1-release-readiness.md` 与 `reports/v1-runner-startup-acceptance.md`。本页只公开必要的工程结果，不分发受控数据库备份、原始执行台账或运行日志。

- 日常迁移完成后，六条保留历史会话的 snapshot、conversation、operations、jobs 和 graph 读取均为 HTTP 200；浏览器逐条切换两个图页，显示真实会话数据。
- 图重复读取结果与 revision 相同，未认证 / 不存在对象分别验证 401 / 404；读取不推进图版本或写入同步回执。
- 通过原 `start-workbench.cmd` 启动正式 API 与完整 Runner，默认启动恢复启用。就绪后观察 184.4 秒、66 次采样，无旧任务领取、租约获取或新增模型调用。
- 两个暂停 Run 与保守保留的未知历史记录未改写；Kali 无活动 Job，源码文件哈希与本机一致，Executor 未重启。

上述结论限于当时的空闲启动、数据保全与历史读取，不证明所有待测任务成功，也不代替本次服务可达性检查。

## 六条历史 CTF 轨迹的证据边界

依据 2026-10-28 的 `reports/six-successful-ctf-runs-review.md`。复盘交叉读取已有 Checkpoint、公开模型/工具消息、Operation、Job、事件、模型调用台账与来源工件，没有重新访问靶场或提交 Flag。原始目标、答案、载荷和运行数据不随 README 分发。

| 等级 | 含义 | 本组观察 |
|---|---|---|
| 工具候选 | 真实工具回执返回 Flag 格式候选，可关联源操作 | R1—R6 均有；候选正确性未由此确定 |
| 独立来源工件 | 同源输出另有内容及 SHA-256 相符的工件 | R4、R5；其余四条仍有独立 stdout 工件缺口 |
| 目标应用通关 | 应用原始正文明确返回全部关卡完成 | 仅 R5 |
| 外部平台判对 | 正式提交接口判对或可信平台完成记录 | 六条均未观察到 |

保存轨迹中六条各仅有一条初始 HumanMessage，未发现人工追加解题路线，可支持“该组记录中的后续动作由 Agent 自主产生”。不能排除模型既有知识、环境残留或题目熟悉度。官方挑战 ID 和实例重置记录不足，不能确认六个独立挑战，更不能按完成候选计算总体成功率。

Decision Memory 与双图有实现、使用和来源记录；缺少足以隔离其净收益的匹配对照，不声称成功率、Token 或成本改善。

## README 截图来源

| 公开素材 | 真实来源与展示范围 |
|---|---|
| [Information Graph](assets/readme/information-graph.jpg) | 2026-10-30 原启动链的隔离 API-only 验收；PostgreSQL 中持久化的 Demo 夹具；未拦截请求或替换 fetch |
| [Attack State Graph](assets/readme/attack-state-graph.jpg) | 同一隔离验收，修改夹具 revision 后通过界面手动刷新读到 v4 |
| [工具执行详情](assets/readme/tool-execution.png) | 2026-10-11 真实前端验收，离线 Demo 工具；历史界面，展示成功/失败、参数与结果脱敏 |

三张图片均在公开前人工查看，未发现真实密钥、Cookie、Token、目标隐私或答案。图片按原始字节复制，不生成替代界面、不拼造成功证据。历史工具截图中的 `api_key` 只显示 `<redacted>`，目标是示例域名。

## 2026-10-31 README 与版本交付检查

本轮仅重写 README、整理展示素材与公开摘要，并将既有正式源码和必要测试整理提交；未修改 Agent、Runner、Executor、数据库、图算法或 Decision Memory 实现。

已完成：锁定依赖安装、生产范围 Ruff、Pyright、前端 66 项测试与构建。工作区正式后端回归为 **1019 passed、2 skipped、1 deselected**，369.50 秒；`-m "not real_model"` 显式排除了真实模型探针，两个跳过仍为 Windows 符号链接权限。

发布文件筛选排除了三份依赖原始 CTF 研究资料的未跟踪测试：`tests/integration/test_graph_update_engineering.py`、`tests/unit/test_graph_update_errors.py`、`tests/unit/test_graph_recovery_transport.py`。它们和原始资料仍留在本地，未删除或修改；工作区 1019 项通过数不能直接等同于公开快照的测试数量。

将待提交文件导出为独立目录后执行正式回归，补齐遗漏的验收辅助脚本、排除上述研究资料依赖后，最终为 **981 passed、2 skipped、1 deselected、0 failed**，230.16 秒。即公开快照执行 983 项测试，另有 1 项真实模型探针被显式排除。测试运行使用独立随机 PostgreSQL 测试库和临时工件目录，未复制日常数据库或研究快照来使测试通过。

README 与本页的内部链接、图片引用和目录锚点均已核对；本机控制台 URL 与启动配置一致，在线可达性见下段。README 架构图通过 Mermaid 解析。默认范围及新增展示文档的 UTF-8 无 BOM 检查通过，Git 待提交文本核对未发现本机令牌、真实密钥或私钥；三张图片与已核验原图字节一致且无 EXIF 元数据。首次装配快照暴露的文件依赖没有通过修改业务实现或历史原始资料处理。

本轮日常数据库连接强制只读。核对时两个 Run 保持暂停、活动租约为 0、模型调用为 298 条；Kali 安装的 23 个 Python 源文件哈希与本机一致，身份与 protocol=1 匹配，执行模式为 trusted、活动 Job 为 0，保留现有进程。8770 API 探针当时不可达，未为截图启动或重启服务；本轮截图复用上节既有验收素材，不声称新拍当前在线界面。

未调用付费模型、重跑 CTF、提交 Flag、修改日常历史状态、重启 API / Runner / Executor，未将 `.env`、备份、CAS、原始 CTF 数据或敏感研究日志纳入提交。原始本轮检查日志保留在本机忽略目录，公开文件仅记录结果。

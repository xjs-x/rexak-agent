# Memory 最终修复：桌面与 Kali 同步验收

日期：2026-10-26。此轮完成部署核验及 Git 提交准备，没有调用真实模型或测试靶场。

## 实际加载与同步

- 原桌面 `Rexak Agent.lnk` 仍直接指向本仓库 `start-workbench.cmd`，工作目录正确，不需要替换快捷方式。
- 通过该入口执行 `--api-only --no-browser --smoke-seconds 30`，启动器正常退出；数据库、SSH、Executor 检查通过，`/health`、`/console/` 均为 HTTP 200。
- 正式 Runner 入口使用 `--once --no-recover --only-run-id`，目标 UUID 已先确认不存在。正常退出并报告没有可推进的 Run，没有恢复或推进旧任务。
- API 启动、健康接口和 Runner 初始化记录一致：`loaded-runtime-v2:7791b577a7eb364ef01d3ba113bc631d9d82742e`；算法 `canonical_code_v2`。Runner 实际模式为 `memory`，`force_off=false`。这是运行进程证据，指纹范围仍是入口及历史辅助核心实现，不代表整个进程。
- 本机依赖已按锁文件同步，前端 API 类型生成及 TypeScript/Vite 构建通过，桌面入口使用新构建。
- Kali 活动部署源码和已安装包的全部 23 个 Python 文件分别与本机逐文件 SHA256 一致；安装文件时间早于当前进程启动时间。无需重新安装或重启。
- Kali 包配置、控制脚本和服务文件与本机一致。补齐远端缺失的 `executor-requirements.txt` 并验证字节一致；只新增依赖清单，没有安装依赖、改变执行配置或重启进程。
- 执行端 `kali-01`，进程 385746，boot_id `385746-db639a43a8e9`，协议 1；健康与认证能力接口均 HTTP 200，active_jobs=0，pip check 通过。

验收完成后本次 API/Runner 检查进程均正常退出；数据库与现有 Kali Executor 保持运行。下次双击原桌面入口即加载本次版本。没有将短时启动检查表述为 API/Runner 当前仍常驻。

## 数据保全

启动前复制旧启动日志到本地验收目录。检查前后，下列生产表的行数与按主键排序后逐行 JSON 的 SHA256 均一致：runs 4059、sessions 4810、tool_operations 3061、tool_jobs 2615、artifacts 4406、model_calls 1012。

`.env` SHA256 一致，仍使用原数据根及工作数据库；Alembic 始终为 `b903eaf18201` 与 `c416a8d2e016`。没有执行新的生产迁移、切换数据库、清理数据卷或改写历史记录。存在历史未决与暂停任务，因此验收没有启动无范围限制的历史恢复。

本地原始验收证据：`data/deploy/memory-final-20261026T065447Z/`。该目录被 Git 忽略，含前后摘要、API 与 Runner 实际版本、Kali 哈希、启动与检查日志；不上传原始运行材料。

## 检查结果

- `uv sync --frozen --all-packages --group dev` 完成；前端生成、TypeScript/Vite 构建通过。
- 本轮全量单元/集成回归：**958 passed、3 failed、2 skipped、1 deselected**，耗时 473.66 秒。明确排除真实模型/真实执行端标记；2 项因 Windows 符号链接权限跳过。Memory、passive、指纹、Runner 等相关回归通过。
- 3 项失败均在 `test_asset_concurrency.py`：迁移后的 schema 已以 canonical endpoint 索引替代 `uq_assets_endpoint_identity`，旧登记路径仍引用原约束，另一个观察断言随之失败。生产库也确认不存在该旧约束。本轮没有修改这条独立资产登记路径或迁移以制造通过；不能声称全量回归全绿，需要独立修复该遗留兼容问题。
- Pyright：0 errors、0 warnings；UTF-8 无 BOM 检查通过（595 个受检文件）。
- 本次暂存的生产、测试与回归辅助 Python 源码 Ruff 通过，遵循项目配置排除迁移目录。全仓 Ruff 仍为 7 项既有研究归档问题，未改写冻结材料；强制单独检查迁移目录另见已有导入排序问题，迁移源码保持原样。
- 暂存文件敏感信息检查通过；提交前检查暂存文件列表及 `git diff --cached --check`。原始验收日志与数据库内容均未纳入 Git。

## Git 提交范围

提交生产历史辅助、双图及其必要既有依赖、运行版本追溯修复、恢复的迁移源码、前端配置入口、确定性回归测试和必要的脱敏固定输入。包含三个工程交付报告，以及本验收记录。

仅收录回归测试确实依赖的少量实验辅助源码与合成场景；原始模型请求、真实历史账本、实验结果、SQLite/SQL 备份、本机配置和凭据保留本地。旧研究报告及冻结材料未改写。示例中的公开本地数据库默认值及本机 secret 路径不是实际 Provider/Executor 密钥；暂存文件已经进行密钥前缀、私钥与实际配置秘密值扫描。

本次没有新增产品逻辑或重新运行安全测试。真实模型理解提示、Memory 收益及靶场效果仍以用户后续运行证据为准。

# Attachment MVP v1 交付记录

日期：2026-10-27。范围：控制端用户附件上传、持久化、只读分析及对话接入。

**代码与隔离环境联调已完成；日常实例尚未切换，不能把本报告当作已上线或真实模型效果验收。** 原有实验目录、测试文件、数据和结果没有编辑或清理。未提交 Git，也未调用真实模型、目标或上传代码。

## 1. 存储与身份

- 复用 `ArtifactStore` 的 SHA256 内容寻址、原子建档和路径边界检查。原件位于 `WORKBENCH_DATA_ROOT/artifacts/<sha[:2]>/<sha>`，解析视图也使用同一 CAS。
- 新增独立 `app.attachments` 业务表。附件不创建 `ArtifactRow`、Operation、Finding 或验证记录，用户资料不会自动成为 Agent 证据。
- 记录 Session、owner、上传幂等键、名称、服务端判定的 MIME、字节数、SHA256、原件/解析存储键、解析状态、上传时间、到期时间、来源及原始消息 ID。
- 上传先得到 `temporary` 附件。发送消息时，在消息提交的同一事务里绑定 `CommandRow.id` 并变为 `bound`；失败则整体回滚。上传重试和消息重试分别使用稳定幂等键。
- 迁移 `a771c28e10f1` 只新增附件表及索引，接在 `c416a8d2e016` 后；保留原来的双迁移分支，不修改历史业务表、框架表或既有数据。

## 2. HTTP 接口

沿用本地登录 Cookie、Origin、CSRF、统一 ApiError 和 `/api` 路径。所有附件路径都包含 Session；后端还核对项目 owner 和软删除状态。

| 方法及路径 | 用途 |
|---|---|
| `POST /api/sessions/{session_id}/attachments?filename=...` | 原始字节流上传；需 `Idempotency-Key` 和 CSRF。服务端逐块限制大小，不信任客户端 MIME |
| `GET /api/sessions/{session_id}/attachments` | 已绑定附件分页；`include_temporary=true` 可恢复未过期暂存资料 |
| `GET /api/sessions/{session_id}/attachments/{attachment_id}` | 元数据及可读状态 |
| `GET .../{attachment_id}/content` | 下载原件；仅通过验证的光栅图片允许 `preview=true` |
| `GET .../{attachment_id}/text` | 页、行、行内偏移及字符预算约束下的只读文本 |
| `DELETE .../{attachment_id}` | 移除未绑定附件；已绑定消息返回冲突，保留历史引用 |
| `POST /api/sessions/{session_id}/attachments/cleanup` | 标记已到期且未绑定的临时附件 |

消息接口新增 `attachment_ids`，提交前不把文件正文拼入用户消息。Session 创建协议新增 `draft=true`：只创建 Session，不启动 Run；新聊天选择文件时使用此模式，发送真实消息才启动 Agent。

## 3. Agent 工具与上下文

工具直接使用控制端数据库和 ArtifactStore，不使用 Executor、Kali 命令或新模型服务。

- `list_attachments(offset=0, limit=20)`：当前 Session 已绑定资料目录，上限 50 条；返回原消息 ID、可读状态、大小、类型、页/行数及 `next_offset`。
- `read_attachment(attachment_id, page=1, start_line=1, end_line=None, char_offset=0, max_chars=12000)`：单页内按行有界读取。超长单行也能通过 `char_offset` 连续读取，不丢弃剩余字符。直接使用返回的 `next_cursor` 参数续读，可跨 PDF 页。
- `search_attachment(attachment_id, keyword, max_hits=10)`：字面关键词搜索，最多 20 条短片段，含页码、行号和来源。不使用正则、Embedding 或外部网络。

来源格式为 `attachment:<id>:sha256:<hash>#page=<page>&line=<line>&char=<offset>`。结果区分 `ok / empty / eof / unsupported / metadata_only / failed / missing`，错误和越权不会返回虚假正文。

`WorkingContextMiddleware` 在既有只读视图中加入最近 5 个已绑定附件的元数据；更多资料由工具分页发现。正文只通过有界 ToolMessage 进入现有 ContextBuilder。系统提示版本为 17，告知自主发现、按需读取、来源引用和低信任边界。

没有新增 Runner、Agent 主循环、Checkpoint 字段或可写记忆。没有修改 Decision Memory、GraphReview 或双图实现。压缩后仍可由 Session 和持久化附件 ID 重读；附件全文和二进制不进入 Decision Memory 或 checkpoint。

## 4. 格式与资源限制

| 类型 | 实现与限制 |
|---|---|
| 文本、源码、JSON/YAML/XML/CSV、常见配置 | 保留原文结构、行号；自动识别 UTF-8 和带 BOM 的 UTF-16/32。其余编码不做猜测性 GBK/Latin-1 兜底，明确报告失败并保留原件 |
| PDF | 本地 pypdf 文本提取，真实页码；加密、损坏、资源超限明确失败。空文本/扫描页明确提示，无 OCR，复杂排版可能丢失 |
| DOCX | python-docx 按正文块顺序提取标题、段落和表格，含 block 编号；不重建版面/页码，不编辑文档 |
| PNG/JPEG/WEBP | Pillow 验证签名、格式及图片完整性，读取尺寸并支持安全预览；当前附件链路只提供元数据，不声称具备视觉理解 |
| ZIP/RAR/7Z、EXE/ELF/DLL/JAR、PCAP/PCAPNG、XLSX、BIN | 仅保存，标记不支持内容解析；不解压、不执行、不当文本交给模型 |
| 其他扩展名、SVG | 拒绝接收 |

配置项均使用 `WORKBENCH_` 前缀：

| 设置字段 | 默认值 |
|---|---|
| `ATTACHMENT_MAX_BYTES` | 20 MiB |
| `ATTACHMENT_MAX_PER_MESSAGE` | 5 |
| `ATTACHMENT_MAX_PER_SESSION` | 100（包含保留记录，避免通过移除绕过配额） |
| `ATTACHMENT_TTL_HOURS` | 24 |
| `ATTACHMENT_PARSE_SECONDS` | 15 秒/文件 |
| `ATTACHMENT_PARSE_MEMORY_MB` | 256 MiB RSS 监测阈值 |
| `ATTACHMENT_PARSE_CHARS` | 2,000,000 字符；超限明确失败，不冒充完整提取 |
| `ATTACHMENT_MAX_PAGES` | 200 |
| `ATTACHMENT_READ_CHARS` | 12,000 字符，配置上限 32,000 |

API 同时最多运行两个解析进程。解析子进程不继承模型密钥或数据库环境变量，超时、RSS 超限会终止进程并返回失败；这属于资源隔离，不是操作系统安全沙箱。DOCX 解压前限制成员数量 2000、总展开大小 32 MiB，并拒绝 XML DOCTYPE；图片限制 2500 万像素。前端选择上限保守固定为 5，后端更低的配置仍会强制执行。

## 5. 前端

保留现有 Composer、聊天布局和视觉结构，接入回形针菜单、文件选择及聊天区域拖拽。卡片显示文件名、类型、大小、XHR 真实字节上传进度；字节上传达到 100% 后单独显示服务端解析阶段。

支持上传失败原因及重试、发送前移除、上传期间阻止发送、重复点击保护、发送失败后保留已上传资料、切换 Session 时中止上传。重试使用原上传身份，消息重试使用原消息身份。消息历史由后端归档携带附件元数据，刷新后恢复；未绑定暂存资料也可从所属草稿 Session 恢复。

健康接口提供 `attachments_enabled` 能力标记。前端只有收到新后端的明确标记才开放附件入口；仍连接旧进程时显示“后端尚未加载附件版本”，避免静态页面先更新后向旧 API 发送不存在的上传请求。此状态下普通文字消息仍可发送，已有组件用例验证。

附件详情包含元数据、SHA256、原件下载、图片预览及分段文本预览。HTML/源码只用 React 转义文本呈现，不注入 HTML。PDF 原件仅下载，文本预览通过提取接口；不内嵌应用同源 PDF 阅读器。

## 6. 权限、安全及生命周期

- 工具只读取当前 Session、当前 owner 下已绑定附件，不能用裸 ID 跨 Session 取文件。临时上传不会自动进入模型可见目录。
- 文件名不作为磁盘路径；拒绝路径分隔符、控制字符等。服务端生成哈希存储键，不能覆盖任意服务器文件。
- 文件类型由扩展名、签名及对应解析器共同判断；客户端 MIME 不作为授权依据。读取工具不接受路径，不执行文件，不解析/请求外部 URL。
- 附件接口与工具均没有写 Scope、TaskAuthorization、Executor 或 confirmed finding 的能力。Scope 仍仅由原用户指令及既有授权流程处理；附件正文和文件名不会送进目标提取入口。
- 提示词注入防护不能保证任意模型永远正确判断。实测证明的是工具授权、无执行路径、无 Scope 写入及上下文来源边界，未宣称完成真实模型对抗效果测试。
- 原件下载加 `nosniff`、`no-store`、CSP sandbox；只有验证过的光栅图片允许内联。日志不输出文件正文。
- 临时附件到期即不可读，清理接口/后续上传会标记到期。已绑定消息不物理删除。共用 CAS 的物理垃圾回收本轮未实现，移除/到期记录及对象保守保留，避免误删历史 Artifact 或并发上传引用。

## 7. 实际验证

所有新增测试使用临时文件和本轮独占空数据库；未执行原实验目录中的实验。原有测试文件没有修改。

| 检查 | 结果与证据 |
|---|---|
| 附件后端/Agent 专项 | 21 passed；`reports/attachment-tests.log`。覆盖格式、哈希、绑定、幂等、跨 Session 拒绝、无效/缺失附件、有界续读、空文件、不支持解析、临时清理、解析进程超时/内存守卫 |
| 官方 create_agent 确定性循环 | 专项中验证目录 → 主动工具调用 → 后续模型请求实际收到带来源正文；清空历史模拟压缩后重读其他段落；外部 Session/路径字符串拒绝；未调用 Executor，Scope 未扩大 |
| 相关旧链路回归 | 57 passed，`reports/attachment-compatibility.log`；另一组 39 passed，`reports/attachment-focused-regression.log`。覆盖原图、焦点、计划、历史辅助及边界 |
| 全量 unit/integration | **1017 passed、3 skipped、4 failed**；`reports/attachment-regression-final.log`。完整全量快照为 1024 项；之后另增两个解析资源守卫用例并纳入 21 项专项通过 |
| 前端组件 | 5 passed；`reports/attachment-frontend-tests.log`。实际运行 React DOM 事件验证选择、拖拽、进度、阻止重复提交、重试身份、移除、切换中止、恢复、XSS 转义及旧后端禁用附件后仍可文字聊天 |
| 独立服务真实 HTTP 联调 | 通过；`reports/attachment-mvp-v1/http-smoke.json`。真实流上传、草稿不启动 Run、消息绑定、SHA256/下载比对、更换 Cookie 后重新查询历史，服务未启动 Runner |
| Pyright | 0 errors；`reports/attachment-pyright.log` |
| 活动源码 Ruff | `uv run ruff check src executor scripts tests` 通过；`reports/attachment-active-ruff.log` |
| 全库 Ruff | 7 个既有实验目录问题，原样保留；`reports/attachment-ruff.log` |
| UTF-8 无 BOM/乱码检查 | 通过；`reports/attachment-encoding.log` |
| TypeScript / Vite | `pnpm --dir web exec tsc --noEmit`、`pnpm --dir web build` 通过；`reports/attachment-build.log` |
| 依赖 | frozen 同步完成；另保留原已安装 benchmark extra，没有移除其运行依赖 |
| diff 检查 | 附件修改无空白错误；全库检查仍有本轮开始前已修改的 `domain/scope.py` EOF 空行，未顺手改动 |

全量失败项没有隐藏或通过改旧测试规避：

1. `test_business_schema_version_rejects_older_and_future_revisions` 写死旧 migration heads；新增表后期望值过时。保留原测试文件，当前 head 是 `a771c28e10f1`、`b903eaf18201`。
2. `test_asset_concurrency.py` 中三个用例：既有 `record_asset` 使用 `ON CONFLICT ON CONSTRAINT uq_assets_endpoint_identity`，既有迁移 `f5c7d0e3b442` 已移除该约束，空库上产生约束不存在及后续观察未提交。附件迁移不修改资产表，未扩展本轮范围修复此冲突。这是源码与本轮失败证据的定位，未另跑修改前基线比较。

3 个 skip 分别为两个 Windows 符号链接权限用例和未配置真实模型的探针。脚本模型上下文替身产生 Pydantic 序列化警告，日志保留。

浏览器 GUI 验收 **NOT_VERIFIED**：computer-use 在选择 Chrome 窗口时因无法可靠确认 URL 而阻止操作，已停止界面自动化，没有用其他浏览器操作绕过。因此组件事件/HTTP 测试不等同于真实浏览器点击、拖拽和布局验收。

## 8. 原入口与部署状态

- 原桌面 `Rexak Agent.lnk` 仍指向 `C:\xjs\rexak-agent\start-workbench.cmd`，启动文件未修改。
- 日常 API、控制台与健康接口仍为 200。其 OpenAPI **尚无附件接口**：现有进程加载的是修改前后端，不能报告已上线。现有启动器持锁，再运行原启动检查受到锁保护而退出（PermissionError）；未强行结束原进程。
- 日常库保留了非终态记录，包括 running/waiting_tool/queued/paused；为遵守“原实验测试数据完全不要动”，没有在日常库执行迁移、启动新的无筛选 Runner、恢复历史任务或重启现有启动器。
- 数据保全只读核对见 `reports/attachment-mvp-v1/preservation.json`：除事件表外所有已核对实体计数相同，未结束 Run 状态快照相同，`.env` 哈希相同。现有服务在两次探针之间自行追加 204 条事件，不能声称数据库逐字节未变化。本次新增测试没有写日常库。
- Kali 已安装 23 个 Python 文件哈希与本机源码一致，身份 `kali-01`、协议 1、能力正常，active_jobs=0，boot_id 两次相同。执行端没有代码变化，因此保留现有进程，无需用户另行更新 Kali。证据见 `deployment-before.json`、`deployment-after.json`。
- 独立临时服务验证了新 API 启动、首页及附件链路，验收后临时服务/随机空库清理完成。真实模型自主分析效果、原桌面入口加载新后端、生产增量迁移及完整浏览器联调仍未验证。

新增迁移意味着下一次新 API/Runner 必须先完成现有 `workbench db-init` 增量迁移；原启动器本来就执行此步骤，不能绕过 schema 检查直接启动。切换时仍应先保留活动任务和备份数据，不删除数据卷或重建日常数据库。

## 9. 最小验证流程

前提：在允许安全停止原实例的维护窗口中备份，并用现有启动器完成增量迁移和新进程启动。本轮没有替用户执行这一可能影响历史运行状态的切换。

1. 从原桌面 **Rexak Agent** 启动，确认 `/openapi.json` 出现附件路径。
2. 新建聊天，回形针选择一份临时 TXT 或 PDF；也可拖入聊天区域。等待卡片从上传百分比转为已保存。
3. 输入“分析附件中的资产并制定下一步计划”。未上传完成时不能发送；发送内容只携带附件 ID。
4. 观察 Agent 根据目录调用 `list_attachments`、`read_attachment`，长文件按 `next_cursor` 续读。
5. 检查最终回答是否引用真实 `attachment:...:sha256:...` 来源及页码/行号。模型效果由用户自行验证；附件中的 IP 不应自动扩大任务授权。
6. 点击历史附件查看元数据、文本片段或图片；下载原件核对内容。
7. 刷新页面或重新进入同一 Session，确认历史附件及消息关联仍在；后续轮次可再次调用只读工具。

可复现的隔离工程验收命令：`uv run pytest tests/unit/test_attachments.py tests/integration/test_attachments.py -q`；`pnpm --dir web exec vitest run src/console/Attachments.test.tsx`。真实 HTTP 服务辅助脚本为 `scripts/attachment_ui_fixture.py` 与 `scripts/accept_attachments_http.py`，前者只创建自己的随机临时空库，不启动 Runner。

## 10. 明确未实现

OCR、图片语义理解、压缩包分析/自动解压、二进制和 PCAP/XLSX 深度分析、附件执行/修改/传输 Kali、RAG、跨 Session 文件库、Office 编辑、共享 CAS 物理垃圾回收均未实现。对无法可靠判断的旧文本编码明确失败；超出提取资源限制的文档保留原件并报告失败。以上限制在接口或 UI 中可见，不冒充解析或执行成功。

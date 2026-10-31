# 二进制附件静态分析 v1 实施报告

日期：2026-10-27；项目：`C:\xjs\rexak-agent`。

P0/P1 代码和确定性工程验证已完成，真实样本工具层验收通过。**真实模型试验未完成；真实浏览器未验证；日常实例未切换。** 不能据此宣称真实模型已经自主解题。

## 1. 新增功能和实际文件

新增五个控制端只读工具，由原 `create_agent` 自主选择。没有第二套 Agent/Runner，没有常驻分析服务，没有 Executor 协议变更。

| 文件 | 内容 |
|---|---|
| `src/agent_workbench/domain/binary/__init__.py` | 分析领域包 |
| `domain/binary/detect.py`、`model.py` | 真实格式、地址映射、版本 |
| `domain/binary/pe.py`、`elf.py` | 结构、节区、导入/导出、PE 资源和 overlay |
| `domain/binary/strings.py` | 有界字符串、分类和位置 |
| `domain/binary/disasm.py` | 函数范围、反汇编、有限引用 |
| `domain/binary/worker.py`、`service.py` | 一次性解析、授权、租约、缓存、分页 |
| `src/agent_workbench/domain/isolated.py` | 提炼原隔离解析运行器 |
| `domain/attachments.py`、`attachment_parser.py` | 复用隔离器，接受无扩展名 ELF 及 sys/so/dylib 原件 |
| `contracts/binary_analysis.py` | 能力/状态协议和 8 KiB 输出预算 |
| `tools/binary.py`、`registry.py` | 五个本地工具，不依赖 Kali 在线 |
| `api/attachments.py`、`app.py` | 分级上传、能力和分析状态 API |
| `persistence/tables.py` | AttachmentAnalysisRow |
| `migrations/versions/d240a68c917e_binary_analyses.py` | 增量新增表 |
| `config/settings.py`、`.env.example` | 独立二进制限制；不覆盖用户 `.env` |
| `agent/prompts/system.py`、`middleware/working_context.py` | 提示版本 18、目录读取提示和信任边界 |
| `web/src/api/client.ts`、`generated/schema.d.ts` | 新 API 和 OpenAPI 生成类型 |
| `web/src/console/Attachments.tsx`、`labels.ts`、`Attachments.test.tsx` | 上传上限、分析状态及交互回归 |
| `pyproject.toml`、`uv.lock` | 解析依赖 |
| `tests/unit/test_binary_analysis.py`、`tests/integration/test_binary_analysis.py` | 格式、权限、预算、缓存、官方 Agent 协议 |
| `tests/support/binary_fixtures.py`、`binary_isolation_probe.py` | 自造字节和无害隔离探针 |
| `scripts/accept_binary_analysis.py` | 随机独立空库中的样本/API/可选模型验收，不启动 Runner |
| `docs/implementation-status.md` | 更新工程和效果验证边界 |

表中简写的 domain/contracts/tools/api/persistence/config/agent 路径均在 `src/agent_workbench/`。未修改 Decision Memory、双图、Runner、Executor 和原实验目录/数据。

## 2. 五个工具

| 工具 | 参数（运行时之外） | 主要返回 |
|---|---|---|
| binary_profile | attachment_id | 格式、位数/架构、入口、节区/导入/导出/资源摘要、overlay、字符串统计、技术提示和局限 |
| binary_strings | attachment_id, query="", kind="any", section=None, min_length=6, offset=0, limit=50 | 文本、编码、文件偏移、可靠 RVA/VA、节区、分类、不同位置和 next_offset |
| binary_functions | attachment_id, name_contains="", min_size=0, offset=0, limit=50 | 名称/生成 ID、start/end/size、来源、符号和边界状态、分页 |
| binary_function | attachment_id, selector, offset=0, limit=120, mode="disasm" | 指令地址/字节/助记符/操作数、有限直接调用、剩余条数和下一页 |
| binary_references | attachment_id, target, limit=50, offset=0 | 引用指令、文件偏移、目标、可靠所属函数、引用类型与扫描局限 |

selector 选择索引中的精确名称或起始 VA。target 支持 VA、`va:`/`rva:`/`file:` 加数值，以及精确函数名或已索引字符串。RVA 只在存在可靠 PE image base 时转换。VA 是静态映像地址，不代表运行期 ASLR/PIE 地址。

返回完整 JSON，包含 status、attachment_id、sha256、`source=user_attachment_derived`、reference、analysis_id、analyzer_version、cached 和 limitations 等。来源形如 `binary:<id>:sha256:<hash>#file=<offset>&encoding=<encoding>`，函数来源另带起始地址和指令位置。

区分 ok、partial、unsupported、failed、missing、not_found、invalid_parameters、pending、busy、too_broad。pseudo/decompile 明确不支持。工具边界**强制实际 UTF-8 JSON ≤8,192 字节**：按完整条目缩页并修正游标/剩余条数，profile 列表结构化缩小并标 truncated；不硬截断 JSON。

## 3. PE/ELF、字符串和函数能力

PE32/PE32+、ELF32/ELF64 按字节识别，不匹配扩展名显式标记；截断、损坏结构和不合理边界失败。PE 支持节区属性、入口/首选基址、子系统、导入及延迟导入摘要、导出、资源统计和 overlay。ELF 支持节区/PT_LOAD、架构/字节序和符号导入/导出，不猜测 overlay，不动态链接。

CLR 头和元数据签名校验后可标记 .NET，但不深入分析 IL。Mach-O、ZIP/JAR、未知及歧义格式只识别/返回不支持，不解压。技术提示以导入或字符串为依据，heuristic 明确标记，不证明语言、凭据有效性或漏洞。

字符串支持 ASCII 和双字节对齐的 ASCII 子集 UTF-16LE/BE，不声称完整 Unicode 恢复。分类为 URL、IP、API 路径、本地路径、错误、配置/协议及普通字符串。连续重复文本有界合并，每组最多 8 个位置；分页仍以原始命中位置计数，保留不同偏移。

基础索引默认最多 100,000 条，完整扫描计数和已索引计数分开。超限明确 truncated。**基础索引不全时，带关键词/类型/节区过滤的查询在隔离 worker 中重新扫描原件，缓存过滤索引**；不会仅搜索部分基础索引后把未命中冒充不存在。过宽查询返回 too_broad，要求过滤。

函数来源严格区分 PE64 异常表、导出表、ELF 函数符号和入口启发式。无可靠表时只给入口候选，未知范围最多 256 字节或至首个 RET；不声称恢复全部函数。DWARF 未实现。

x86/x64 函数反汇编按需生成，单函数最多 1 MiB/100,000 条指令；默认每页最多 120 条。其它架构可识别/列结构，反汇编和引用不支持。引用是线性扫描，识别 RIP-relative、直接调用/分支和有限地址候选；最多扫描 8 MiB 可执行原始数据、500,000 条指令、100,000 条引用。重叠范围不伪造唯一所属函数。间接调用、虚表、跳转表、代码内数据、混淆/壳可能导致遗漏或误判，未找到不能推断未使用。

## 4. 存储、缓存与授权

原件继续存既有 ArtifactStore CAS，不修改。结构化索引也是 CAS JSON，最大 16 MiB。新增 `app.attachment_analyses` 记录附件/Session/owner、源 SHA256、分析类型/引擎/版本、规范化参数与哈希、状态/租约/attempt、摘要、结果键/结果 SHA256、错误类型和时间。

内部类型为 base、过滤 strings、code、单函数 function；五个工具复用索引。缓存键包含 attachment_id、源 SHA256、kind、引擎及其版本和参数哈希；版本包含 pefile/pyelftools/Capstone 版本，资源配置也参与参数。分页不重复生成同一分析。

每次调用按当前 Session、owner、未删除项目/Session 和 bound 状态检查，缓存命中及完成后仍检查。跨 Session 同 SHA256 使用独立记录，只允许底层 CAS 共享。工具只接受附件 UUID，不接受路径/CAS 键。UI 状态查询也执行附件权限校验。

短事务领取唯一分析记录/租约，相同分析并发返回 pending；失效 pending 租约可重领，失败可由后续显式调用重试。跨 API/Runner 用 PostgreSQL 会话 advisory slots，默认最多 2 个 worker，解析期间无打开的数据库事务。取消要等待 worker 回收才释放槽。CAS 原子写成功后才提交 ready，并验证源/索引哈希和 worker 版本。

本轮共享 CAS，**不创建 ArtifactRow、Operation、Job 或 Finding**。analysis_id 不成为可绕过证据规则的 artifact_id，未放宽 confirmed/propose_finding 校验。数据始终是 user_attachment_derived 静态观察，不是漏洞证据或授权。

## 5. 隔离和剩余风险

原 `_parse_isolated()` 委托通用 `domain/isolated.py`，保留原附件失败结构和原文档资源限制，原附件回归通过。二进制默认 30 秒、RSS 512 MiB、输出 16 MiB、2 个并发槽。服务端决定模块和附件路径，不经 shell；环境白名单不包含数据库/模型密钥；stdout/stderr 不回传正文，异常/超时/内存/输出超限回收进程。

worker 加载受信库后安装 Python 审计钩子，拒绝 socket、进程启动和输出目录外写入。实际测试覆盖这些拒绝、环境隔离、超时、崩溃和输出超限；原资源守卫测试覆盖模拟 RSS 超限。

**不是完整 OS 沙箱。** Windows worker 仍是当前用户身份，未实现受限令牌、只读挂载、内核网络/系统调用隔离；审计钩子不能当作原生扩展漏洞的安全边界，RSS 是轮询阈值而非内核硬配额。报告不宣称强沙箱。

## 6. 上传、API、前端和依赖

普通附件仍默认 20 MiB，仅真实头部符合 PE/ELF 的附件允许 64 MiB。流接收先受最大额度约束，再按实际头部执行分类额度；浏览器 MIME 不决定额度。文本伪装仍拒绝。二进制保存后由工具按需分析，不走全文文本解析或执行。

新增 `GET /api/attachments/capabilities`（大小/数量/格式）与 `GET /api/sessions/{sid}/attachments/{aid}/analyses`（有界真实状态/摘要）；过期 pending 租约显示 interrupted。原上传/绑定/下载路径不变，二进制仅下载，不内联运行。

前端保留原布局，读取服务端限制，显示普通/二进制额度，支持超过普通额度的程序上传和明确超限失败。详情显示未分析、分析中、已分析、失败/中断，摘要按 React 文本转义；现有工具活动展示反汇编/引用，没有虚假反编译。原文本、PDF、DOCX、图片路径保持。

配置均带 `WORKBENCH_` 前缀：BINARY_ATTACHMENT_MAX_BYTES=67108864、BINARY_ANALYSIS_SECONDS=30、BINARY_ANALYSIS_MEMORY_MB=512、BINARY_ANALYSIS_OUTPUT_BYTES=16777216、BINARY_ANALYSIS_CONCURRENCY=2、BINARY_ANALYSIS_MAX_STRINGS=100000。未放宽原文档解析时间/字符/页数/内存。

Windows 实装 pefile 2024.8.26、pyelftools 0.33、Capstone 5.0.9，锁文件已更新；保留原可选 benchmark extra。未安装 Ghidra、radare2 或 Wine。

## 7. 真实样本工具验收

只读取既有桌面 Raphael_Terminal.exe，使用前核对 SHA256：`5c09f586174c0afd563eea3f69699a94e3447037e47590e0c28fe2f003cc93ca`。未下载、未执行、未传至 Kali、未请求其中 URL。

| 项目 | 实测 |
|---|---|
| 上传 | 27,990,016 字节，超过原 20 MiB，真实 PE MIME，保存/绑定成功 |
| 结构 | PE32+、x86-64、Windows GUI、7 个节区 |
| 资源 | .rsrc 文件数据 27,019,776 字节 |
| 导入 | 9 个库、187 个函数，含 WININET 线索 |
| 字符串 | 观察 353,952 个字符串块，基础索引受 100,000 条限制并明确 partial |
| Web 线索 | 实际检索到回环 HTTP 地址、archive API 路径和 maintainer 参数 |
| 函数 | 异常表恢复 2,647 个范围，不声称全部函数 |
| 引用 | 线性扫描 737,280 字节/197,313 条指令，记录 39,067 个有限引用 |
| 深入 | API 路径引用定位 sub_140032bf0，声明范围 6,492 字节，生成 1,526 条反汇编指令 |
| 恢复 | API 上传→绑定→下载核对→新 Cookie 历史恢复通过，独立测试库已清理 |

证据在 `reports/binary-analysis-v1/` 的 sample-p0.json、sample-p1.json、http-acceptance.json、http-acceptance-final.json。地址/预期只在测试脚本、Fixture 和报告中，没有进入正式 System Prompt。报告 UUID 属于已清理的隔离库，不指代日常实例业务对象。

## 8. Agent 协议与真实模型结果

确定性测试使用官方 create_agent、原 WorkingContext/ContextBuilder、真实工具和独立 PostgreSQL，验证目录→profile→strings→functions→references→function→清除历史后续读。后续模型请求实际含工具来源；首次只含目录，二进制 ToolMessage ≤8 KiB。Scope 不变，未产生远端操作/Job/ArtifactRow/Finding；伪系统文本只进入低信任 ToolMessage，没有进入系统权限或目录记忆。该结果证明协议和授权，**不是模型策略或抗注入效果证明**。

真实模型只做一次独立 Session 试验，使用当时配置的 **mimo-v2.6-flash**，没有切为 DeepSeek。通用任务指令没有预置答案，仅开放附件和五个静态工具，SDK 重试为 0。

8 个物理请求后触及验收图 `recursion_limit=32`，报 GraphRecursionError，**NOT_COMPLETED**，未取得最终回答。它是测试图步数边界，不能解释为模型语义失败或自主分析成功。首次脚本只在最终返回时汇总 ToolMessage，异常时未保留中间轨迹，故**无法复核是否找到线索或满足 ≤8 次二进制工具目标**。报告中预先执行的工具检查不能冒充模型已经读取。

之后修正验收脚本为逐图状态保留工具名/状态/来源、计数和用量，排除隐藏推理和请求正文，并有隐私单测；未重新付费重放。原始记录见 real-model-acceptance.json / binary-analysis-real-model.log。首轮异常未汇总完整 token/费用，记为未核实，不代表费用为零。没有 DeepSeek 真实效果结论。

## 9. 测试结果

所有数据库验证使用本次独占随机空库，不领取日常队列；自造字节不执行。

| 检查 | 结果 |
|---|---|
| 开发前旧 schema/asset 并发基线 | 7 passed、4 failed |
| P0（含原附件） | 30 passed |
| P1 初始定向 | 20 passed |
| 最终定向（含全文件过滤、预算、去重、Agent 和原附件） | 46 passed，保留 Pydantic 测试替身 warnings |
| 首轮全量 unit/integration | 1,040 passed、3 skipped、4 failed，435.77 秒 |
| 最终全量 unit/integration | 1,044 passed、3 skipped、4 failed，475.54 秒；退出码 1，完整回执已保存 |
| Ruff src/executor/scripts/tests | 通过 |
| Ruff 全仓 | 既有实验树中 7 项问题，未修改冻结代码制造通过 |
| Pyright | 0 errors、0 warnings |
| 编码 | 650 个文件通过 UTF-8 无 BOM/无乱码检查 |
| 前端 Vitest | 7 passed |
| tsc --noEmit / pnpm build | 通过 |
| 真实浏览器 | NOT_VERIFIED：Computer Use 无法可靠确定 Windows 浏览器当前 URL，工具停止；未绕过 |

原 4 个失败：test_business_schema_version_rejects_older_and_future_revisions 硬编码旧 heads；test_asset_concurrency.py 三项涉及既有 uq_assets_endpoint_identity 约束与迁移不一致。本轮新迁移进一步改变 head，但基线在新迁移前已复现同名失败。未修改旧断言、删除数据或修复范围外 asset 逻辑来伪造全绿。3 个 skip 是 Windows 符号链接权限两项和没有环境 Key 的旧真实模型探针；这不否定本轮独立模型试验确实发生。

命令输出见 reports/binary-analysis-*.log，失败名称对比见 regression-comparison.json。

## 10. 迁移、Kali、启动和安全切换

新增 revision d240a68c917e，父 a771c28e10f1；既有另一 head b903eaf18201 保留。独立测试库已通过原 workbench db-init 增量迁移验证。没有在日常库迁移或重建，没有删除数据卷。

桌面快捷方式、start-workbench.cmd、启动脚本未变。新 API/Runner 需要迁移和重新加载源码；原启动器已有 db-init 流程。本轮日常 API 探针是 ConnectError，数据库保留 2,802 条非终态记录；未强行重启旧服务、取消历史 Run 或启动会领取旧队列的 Runner，**不能报告已上线**。

Kali 23 个安装 Python 文件与本机 Executor 源码哈希一致，kali-01 身份符合；没有执行端改动，不需要更新或重启。前后证据见 deployment-before.json / deployment-after.json，preservation.json 确认前后已核对的表计数、非终态快照、.env 和 boot identity 相同。

安全切换：先核对真正活动的任务、租约和远端 Job，明确历史排队项恢复计划；按既有流程备份数据库、checkpoint/Store 和数据目录。维护窗口安全停止旧实例，安装锁定依赖（保留 benchmark 时 `uv sync --frozen --all-packages --group dev --extra benchmark`），用原 `uv run workbench db-init` 增量迁移。可先 `start-workbench.cmd --api-only --no-browser` 验证 API 和上传，确认历史队列恢复方案后再正常启动 Runner。

最小用户验证：新 Session→上传程序→发只读分析任务→观察真实二进制工具活动及 binary/sha256 来源→刷新查看历史附件。实际模型能否自主找到线索须观察，不套用确定性结果。

## 11. 未实现和后续

不提供反编译、动态执行/调试、Ghidra/radare2 服务、解壳、完整 CFG/调用图/间接引用、完整 Unicode、DWARF、跨 Session 知识库或附件远端传输。资源上限外保留 partial/too_broad。

下一步优先 OS 级只读/无网络隔离、更多架构/DWARF、可恢复大型索引以及有完整中间轨迹的真实模型评估。真实浏览器受工具限制未完成，真实模型本轮未完成，因此不宣布最终自主效果验收通过。

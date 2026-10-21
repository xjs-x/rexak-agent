# Skill 模块使用与实现说明

2026-10-21。当前版本仅接入用户自行提供的外部方法论。此前“宿主 methods 转为 builtin Skill”的安排已撤销。实现位于 `C:\xjs\rexak-agent`，使用现有官方 `create_agent`、checkpoint、工件存储和 Python Kali Executor。当前验证见[外部 Skill 边界恢复验收](acceptance-results/2026-10-21/external-skills-regression.md)。本文不提供具体渗透 Skill 内容。

## 与原循环的边界

原有计划、工作摘要、弱点/线索、证据、结束判定以及 `search_methods` 继续由原体系负责，不导出成 Skill，不由 Skill 接管。Skill 的来源只包括用户目录和主动导入的 ZIP。

没有发布可用外部 Skill 时，新运行保持原工具集合和中间件，不出现 Skill 目录、默认 Skill 提示或额外目录工件。启动运行时只做一次库存发现；不在每次模型请求前查询空 Skill 库。页面 GET 列表不再触发扫描或导入。

用户显式刷新或导入外部包后，后续运行才按固定目录启用可选 Skill 工具。模型可以按任务匹配这些外部资料，用户也可明确选择；没有适用方法时继续原任务，不要求先加载 Skill 才能工作。

旧版本自动生成的 `builtin:` 转换项已退出可用目录，禁止加载、执行、重新启用或绑定项目；历史版本与原件保留供审计。旧 checkpoint 停在原 Skill hook 时，使用无内容、无数据库操作的兼容节点继续，不重放工具。状态 schema 保留 v5 兼容已保存任务，不做破坏历史的降级或清库。

## 直接添加自己的 Skill

默认用户目录为 `WORKBENCH_DATA_ROOT/skills/user`；配置 `WORKBENCH_SKILLS_ROOT` 时使用指定目录。以下以数据根目录为 `data` 举例：

```text
C:\xjs\rexak-agent\data\skills\user\
  my-skill\
    SKILL.md
    references\        # 可选
    scripts\           # 可选
    assets\            # 可选
    rexak.yaml         # 可选
```

最小入口文件结构：

```markdown
---
name: my-skill
description: 说明此技能适用的任务、触发条件和用途。
metadata:
  version: "1.0"
---
在这里维护你自己的指导正文。
```

文本使用 UTF-8 无 BOM。`name` 为小写英文字母、数字和单连字符，最多 64 字符；目录导入时与目录名一致。`description` 为 1–1024 字符。正文必须非空。`references/`、`scripts/`、`assets/` 均可省略，不需要修改 Python 注册表。

控制台侧栏进入“技能库”，点击“刷新目录”；也可以上传 ZIP，或输入已配置用户根目录内的目录路径后导入。ZIP 可直接包含 `SKILL.md`，也可包含一层外包装目录。页面可以检索、查看文件和历史版本、导出 ZIP、启停、绑定项目可见性及项目默认技能。

页面勾选的 Skill 随下一条消息或新任务以结构化引用明确提交，提交成功后清空本次选择。仅勾选不会启动 Agent 或执行脚本。模型也可以从名称和描述中自主选择；此机制是否提高实际任务效果仍需真实模型实验。

可通过 `.env` 调整用户根目录和上下文预算：

```dotenv
WORKBENCH_SKILLS_ROOT=C:\my-skills
WORKBENCH_SKILL_CATALOG_TOKENS=2000
WORKBENCH_SKILL_CONTEXT_TOKENS=12000
```

第一版支持一个配置根目录。源目录中的 `.git`、`node_modules`、`.venv`、`venv`、`__pycache__` 不发布；不要把运行产物存入技能源目录。修改配置后需重新启动控制端。

## 选择、加载与上下文

| 工具 | 作用 |
| --- | --- |
| `search_skills` | 查询本次运行固定的目录；关键词检索和分页，不读取所有正文 |
| `load_skill` | 固定指定技能及版本；下一次实际模型请求注入完整正文，不执行脚本 |
| `read_skill_resource` | 按活跃版本读取附件；正文和文件清单分别分页，保留文件哈希 |
| `unload_skill` | 移除后续请求中的指导引用，保留历史和已有进程 |
| `run_skill_script` | 在 Kali 上执行固定版本中的脚本，复用原命令执行台账 |

Skill 不是独立 Agent，也没有额外分类模型。发现时只展示有界元数据，活跃正文由上下文中间件每轮从固定包重建，附件按需读取。加载确认消息不重复保存整篇正文。压缩和进程恢复依靠 checkpoint 中的版本引用，而不是依赖旧 ToolMessage 恰好仍在上下文中。

同时最多 20 个活跃技能。Skill 专用预算使用字符估算，整体请求仍经过现有模型预算估算和压缩机制；长正文超限会明确拒绝加载，不截去中间规则。目录未展示的项可搜索；资源正文每次最多 16000 字符，文件清单每页 50 项，用 `next_file_offset` 继续读取。

Skill 的用户指导不能修改宿主系统约束、目标范围和事实确认规则。只接受用户消息中的结构化选择引用，不把网页、工具输出或文件中出现的 `$name` 当作用户授权。第一版没有自动解析聊天文本中的 `$name`。

## 脚本机制

没有脚本时不需要 `rexak.yaml`。普通 `.py`、`.sh`、`.js` 可以直接按 `scripts/` 下的相对路径调用。需要入口别名或依赖提示时，可添加：

```yaml
schema_version: 1
requires:
  binaries: [python3]
  capabilities: [command]
entrypoints:
  main:
    path: scripts/main.py
    interpreter: python
```

解释器类型支持 `python`、`shell`、`node`。Python 和 shell 使用执行端配置的 `EXECUTOR_PYTHON_INTERPRETER` / `EXECUTOR_SHELL_PATH`，Node 从执行端 PATH 发现。依赖不会自动安装；缺解释器、二进制或声明能力时返回确定错误。

脚本启动前同步整个不可变包，包含相对 import 所需的其他脚本、参考资料和二进制资源。首次使用上传 ZIP；缓存命中只发送包标识，不重复传输整包。执行端验证包哈希并原子物化到会话工作区 `.rexak/skills/<package_hash>/`，脚本启动再次核对包完整性和脚本路径。文件工具拒绝改写该缓存；修改原始内容应重新发布版本。

脚本通过 argv 传参，可选 JSON stdin；控制端不拼接 shell，也不执行包内代码或安装 hook。脚本获得：

| 环境变量 | 用途 |
| --- | --- |
| `SKILL_DIR` | 固定版本的完整资源目录 |
| `SKILL_OUTPUT_DIR` | 当前逻辑调用的产物目录，也是默认 cwd |
| `WORKBENCH_WORKSPACE` | 本会话执行工作区 |
| `PYTHONDONTWRITEBYTECODE` | 避免 Python 把缓存写入版本目录 |

产物路径为 `skill-output/<稳定调用标识>/`，不同调用使用不同目录。脚本应把新文件写入该目录，不修改包。标准输出和错误沿用原日志能力，通过 `read_output` 查看、`export_execution_output` 保存原始日志区间；文件产物通过现有 `read_file` 等工作区工具读取。第一版不会自动把整个输出目录无限上传到控制端。

`background`、`wait_ms`、单次执行截止、查询、取消和服务重启恢复沿用现有命令机制。同一 `run_id + tool_call_id` 复用操作身份；回执不确定时核对原操作，不自动创建第二次执行。卸载指导或停用技能不等同于终止已运行的脚本。

执行需要执行端明确声明 `skill_packages=available` 和 trusted 命令能力。可信执行不是操作系统沙箱；`network_isolation=unavailable` 保持如实展示。Skill 文件本身不能授予新工具、提权或改变这些策略。

## 版本、生效与恢复

完整包的路径、长度和原始文件 SHA-256 决定 `package_hash`；正文、脚本或附件任一变化都会产生新版本。时间戳、ZIP 时间和本机绝对路径不参与内容身份。相同内容再次刷新复用原版本，`metadata.version` 仅是可读标签。

- 每个 run 在装配时发现已发布的外部库存；非空目录固定快照，运行中不重新扫描源文件。空目录不生成目录工件或增加 run 配置。
- 会话活跃 Skill 固定 `version_id + package_hash`，续跑时保留原版本。
- 刷新或页面选择历史发布版本影响后续运行的目录；已有活跃指导需通过明确加载指定版本切换。
- 停用和收紧项目可见性会阻止旧快照继续读取或派发；已有引用显示不可用，本轮不注入正文。
- 源目录删除不会删除已发布包；需要停用时在技能库执行停用操作。
- 历史包不自动删除；第一版不提供破坏历史引用的垃圾回收。

状态 schema 从 v4 升至 v5，旧状态经过显式迁移，保留消息、计划、待执行节点和操作身份。不能安全迁移的未提交并行结果会拒绝恢复并要求核对台账，不猜测重放。

## 数据与复盘

| 数据 | 权威归属 |
| --- | --- |
| 安装配置、当前发布指针、项目可见性 | `app.skill_installations` |
| 不可变版本及包工件引用 | `app.skill_versions` |
| 包原件、运行目录快照 | 现有内容寻址工件存储 |
| 活跃版本和最近消费的选择指令 | LangGraph checkpoint |
| 实际进程、输出和恢复 | Executor 原台账 |

`skill.activity` 事件记录加载、附件读取、卸载和执行请求；实际呈现的版本进入 context manifest。普通远端操作记录 `contextual_skills`，Skill 脚本另记录 `explicit_skill`；这只说明指导或执行关联，不推断哪个技能贡献了结论。

私有运行包导出包含目录快照、引用版本和完整 ZIP，写入 `skills/index.json`、`skills/catalogs/`、`skills/packages/` 并纳入哈希核对。缺失依赖会明确记录，不从当前源目录补造历史内容。正文样例和脚本输出都不会直接成为 confirmed 发现。

## CLI 与管理接口

从项目目录执行：

```powershell
uv run workbench skills rescan
uv run workbench skills list
uv run workbench skills validate C:\my-skills\my-skill
uv run workbench skills import C:\my-skills\my-skill
uv run workbench skills import C:\packages\my-skill.zip
uv run workbench skills export <skill_id> C:\exports\my-skill.zip --version <version_id>
```

目录导入必须位于配置根目录内；`validate` 可独立校验指定目录或 ZIP。导出不会覆盖既有文件。

API 复用本机会话、Origin 和 CSRF：

```text
GET   /api/skills
POST  /api/skills/rescan
POST  /api/skills/import
GET   /api/skills/{skill_id}
GET   /api/skills/{skill_id}/versions
PATCH /api/skills/{skill_id}
GET   /api/skills/{skill_id}/versions/{version_id}/files
GET   /api/skills/{skill_id}/versions/{version_id}/export
GET   /api/sessions/{session_id}/skills
```

安装包最多 1000 文件、单文件 8 MiB、文件总量和 ZIP 各不超过 32 MiB。拒绝路径穿越、设备路径、文件/目录冲突、大小写冲突、链接、特殊文件、非法文本编码和重复 YAML 键；源文件扫描窗口内变化时拒绝发布。

旧 `search_methods` 及其内容保持独立，不经过 Skill adapter、不自动发布。未知 `allowed-tools`、MCP、产品专用 frontmatter 等显示诊断并忽略，不授予能力；未知 `rexak.yaml` 运行字段明确拒绝。Git 自动更新、MCP 工具适配、插件市场、自动依赖安装和参数 JSON Schema 校验不在第一版范围。

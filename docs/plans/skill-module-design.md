# Rexak Skill 模块完整设计方案

日期：2026-10-17。原设计提案已进入代码落地；实际第一版机制、明确边界与操作方法见 [Skill 使用说明](../skills.md)，验证范围见[验收记录](../acceptance-results/2026-10-17/skills/README.md)。下文保留设计依据，不将设计条目本身当作验证证据。

本方案设计 Skill 的发现、选择、加载、资源读取、脚本执行、版本更新和运行恢复机制，不编写具体测试 Skill 内容。依据当前工作区（基线提交 3f6d258，包含已有未提交修复）及当日读取的官方文档制定。

## 1. 建议采用的方案

采用 Agent Skills 通用目录格式，以 `SKILL.md` 为入口；通过目录元数据发现、模型按需加载、附件按需读取实现渐进加载。正文作为主 Agent 的任务方法指导，脚本通过现有 Kali Executor 执行。继续使用官方 `create_agent`，不增加 Skill 专用 Agent 主循环。

用户的最小使用流程：创建一个 Skill 目录并写入 SKILL.md → 放入已配置的个人 Skill 根目录 → 页面点击刷新（或 CLI 刷新）→ 校验后发布不可变版本 → 后续运行自动发现，或在任务中明确指定使用。

默认支持用户自编 Skill；不要求插件市场、MCP、向量数据库、额外路由模型或每次运行前的人工批准。执行权限沿用当前任务与 Executor 配置；指定 Kali trusted 模式的授权不因加载 Skill 重新逐命令确认。

## 2. 参考实现与取舍

| 参考 | 实际机制 | Rexak 的取舍 |
| --- | --- | --- |
| ARTEX/norma | 扫描 SKILL.md；Skill 元工具加载正文；目录预算；支持 Skill 关联 MCP、延迟工具及历史恢复；压缩后重新注入方法指导 | 借鉴目录、按需调用、恢复与使用记录；以 Python 实现，补齐版本固定和远端执行 |
| Codex | 先展示名称与描述，选择后加载完整正文；支持显式指定与依据描述选择；文件夹包含脚本和参考资料 | 借鉴标准格式与两种触发方式，不照搬产品专用路径或私有配置 |
| Claude Agent Skills | 元数据、正文、附件分层加载；脚本执行结果进入上下文，脚本源码无须全部进入上下文 | 借鉴资源组织和按需读取，执行环境使用 Rexak Executor |
| LangChain | 单 Agent 通过 load_skill 工具读取专门指导，可与 create_agent 组合；Deep Agents 提供更完整的中间件 | 使用当前 create_agent 的工具和 middleware 接缝，不为 Skill 切换整个 Agent 框架 |

参考事实与本方案的版本快照、执行岛同步、权限和界面设计分开；后者是为 Rexak 提出的产品设计，不声称属于通用标准。

官方来源（2026-10-17 已打开正文）：

- Agent Skills 规范：<https://agentskills.io/specification>
- Codex/ChatGPT Skill 官方文档：<https://learn.chatgpt.com/docs/build-skills>
- Claude Agent Skills：<https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- LangChain Skills：<https://docs.langchain.com/oss/python/langchain/multi-agent/skills>
- Deep Agents Skills：<https://docs.langchain.com/oss/python/deepagents/skills>

本地依据：`D:/ARTEX/norma/skill/skill.go`、`D:/ARTEX/server/assembly.go`、`D:/ARTEX/agent/deferred.go`；Rexak 的 `agent/factory.py`、`agent/prompts/methods.py`、`tools/knowledge.py`、`tools/commands.py`、`tools/execution_dispatch.py` 和 `agent/middleware/context_builder.py`。

## 3. 职责边界

| 对象 | 负责 | 不自动获得的能力 |
| --- | --- | --- |
| Skill | 方法、流程、参考资料、脚本和模板 | 修改任务范围、授权权限、确认漏洞、直接结束任务 |
| Tool | 执行一次确定操作 | 将执行成功解释为漏洞成立 |
| 当前任务状态 | 计划、焦点、活跃 Skill 引用 | 保存整包文件或进程句柄 |
| Observation/Lead/Finding/Verification | 观察、假设、发现与验证记录 | 把 Skill 示例内容视为本次证据 |
| 长期 Store | 跨任务经验 | 承担 Skill 发布库或当前任务状态 |
| Executor | 脚本、依赖环境、进程、输出和真实执行状态 | 持有模型 Key 或控制端数据库凭据 |

Skill 提供的完成标准用于指导模型。阶段结束仍由现有结束提议与 FinishGate 判定；漏洞确认仍走验证记录。Skill 加载成功、脚本退出码为零、发现已验证是不同事件。

## 4. 文件格式与兼容范围

### 4.1 最小可用包

```text
my-skill/
  SKILL.md
```

```yaml
---
name: my-skill
description: 描述何时使用这个技能、完成什么，以及不适用的情形。
---

这里由用户编写技能正文。
```

这两个字段足以让纯说明 Skill 被发现和使用。描述和正文允许正常中文；名称遵循通用规范并与目录名一致。建议作者使用小写英文、数字、连字符，解析校验与所固定的规范版本一致。

### 4.2 可选扩展目录

```text
my-skill/
  SKILL.md
  references/
  scripts/
  assets/
  examples/
  requirements.txt       # 可选，依赖声明资料
  package.json          # 可选，依赖声明资料
  rexak.yaml            # 可选，Rexak 运行提示
```

根目录以外的普通文件也允许存在；references/scripts/assets 是约定而非必需结构。增加附件不要求修改 Python 注册表。

标准字段支持 name、description、license、compatibility、metadata；metadata 保持字符串键值映射，版本标签可放在 metadata.version。`allowed-tools` 属实验字段：导入时识别并显示，不解释为用户授权，也不冒充已完整支持其他产品的工具名和模式语法。

`rexak.yaml` 是可选扩展，仅用于声明所需能力、解释器、二进制依赖、入口别名、参数 schema 和产物提示。它不是安装脚本或权限授予文件。普通脚本位于 scripts/ 时无需先声明入口；显式传相对路径即可运行。

例如：

```yaml
schema_version: 1
requires:
  capabilities: [script]
  binaries: [python3]
entrypoints:
  main:
    path: scripts/main.py
    interpreter: python
```

这里的入口只是格式示例，不提供测试内容。依赖 schema、解释器映射和未知字段处理在 contracts 中定义，未知必需扩展返回 incompatible，不静默忽略。

### 4.3 导入兼容性

兼容的是目录与渐进加载机制，不承诺其他 Agent 的产品专用字段、工具名和运行环境原样可用。导入 Codex/Claude Skill 时，对专用宏、工具依赖和平台字段给出兼容诊断。

例如 Claude 的 fork/context、Codex 的 agents/openai.yaml 或 ARTEX 的 mcps 不直接产生新 Agent 或连接新服务。V1 作为已识别但未实现的扩展报告；纯文本可读部分可以使用，依赖未支持功能的部分标记受限。

## 5. 保存位置、来源与版本

### 5.1 用户维护与运行数据分开

建议配置 `WORKBENCH_SKILLS_ROOT`，默认位于 `WORKBENCH_DATA_ROOT/skills/user`。也可把用户自有 Skill 根目录显式加入配置。应用内置资料放在 Python 包的 `skills_builtin/`，随应用版本发布。

```text
WORKBENCH_DATA_ROOT/
  skills/
    user/                 # 用户可编辑源目录
  artifacts/              # 复用现有工件存储，保存不可变包与目录快照
```

后端可支持多个已配置根目录。扫描只覆盖这些管理目录，不扫描下载目录、目标站点文件或任意任务输出；Agent 不能通过生成 SKILL.md 自动把文件变成可信安装包。

技能内容由用户源目录维护；发布后不可变包是运行时内容依据。数据库只管理安装配置和发布版本引用，不建立第二份可编辑正文。

### 5.2 不可变内容版本

每次刷新：安全读取并校验源目录 → 构造排序文件清单与每文件 SHA256 → 计算 package_hash → 原子保存完整包与 manifest → 事务提交版本与当前发布指针。

package_hash 使用规范化相对路径、文件字节长度与原始字节哈希，忽略 mtime、压缩包时间戳和本机绝对路径。不对源文件静默转换编码或换行。同一内容重复刷新复用同一版本；脚本、参考资料、模板的变化也必须改变包版本。

metadata.version 是人类标签，package_hash 是实际内容身份；相同标签不同内容仍是不同版本。

安装项有稳定 skill_id，来源和显示名称分开。跨来源同名不覆盖；UI 显示 user:my-skill、builtin:my-skill 等限定名，模型最终调用使用 skill_id。已保存任务不会因后来出现同名包而改用其他内容。

### 5.3 更新生效

- 新增、修改、启停默认影响下一个 run 的目录快照。
- 正在运行的 run 固定 catalog_snapshot_id，避免选择目录中途变化。
- 当前活跃 Skill 固定 version_id/package_hash；同一 session 续跑时继续原版本。
- 新 run 可以看到新发布版本，但已有活跃 Skill 升级必须显式切换版本，在已闭合工具批次边界生效。
- 页面可发送“加载指定 Skill/切换版本”指令；Runner 按现有 command 机制消费，不绕过图状态写入。
- 紧急撤销属于独立管理动作，覆盖旧快照的使用许可，立即阻止新读取或派发；不能声称已经终止所有历史远端进程。
- 卸载先取消未来可用性；历史 run、checkpoint、操作及归档引用的包保留，GC 检查所有引用后才能删除。

## 6. 发现与触发

### 6.1 三层加载

| 层级 | 加载内容 | 时机 |
| --- | --- | --- |
| 目录 | skill_id、名称、简短描述、版本、适用/能力状态 | 运行开始及模型请求中的有界目录 |
| 正文 | 指定版本 SKILL.md 方法指导 | 用户指定或模型选择后 |
| 附件 | 参考资料片段、模板、脚本文件 | 当前步骤需要时读取或执行 |

目录本身占用上下文；不使用“安装多少都没有成本”的表述。建议初始目录预算为估计 2000 tokens，可配置。超出时先展示任务明确指定项与相关项，并附总数、遗漏数量和完整搜索入口；不能静默宣称已经展示全库。

检索 V1 使用名称、描述、可选标签的 Unicode 文本匹配与稳定分页；中文子串可工作，不采用仅英文正则切词。候选检索是检索，不是最终适用性裁判；模型读取描述并决定。第一版不增加独立路由 LLM、向量库或硬编码漏洞类型路由。

### 6.2 三种触发来源

1. 用户明确指定：UI 选择 Skill，或消息附带结构化 skill_refs；兼容 $name 只是 UI/后端的便利解析，不从网页或工具输出解析同等指令。
2. 模型自主选择：从目录或 search_skills 得到候选，调用 load_skill。
3. 项目固定指导：用户配置的默认 Skill，在 run 初始化时经同一加载服务接入。

用户明确指定且可用时必须加载；不存在、禁用、歧义或不兼容时给出具体原因。不要把“显式指定已加载”误称为“模型自动发现成功”。自动选择不承诺每次一定触发，必须通过适用与不适用样本验证。

## 7. Agent 工具接口

以下是拟议接口，尚未实现。

| 工具 | 输入要点 | 行为 |
| --- | --- | --- |
| search_skills | query 可空、cursor、limit | 返回可见目录；不读取所有正文 |
| load_skill | skill_id、可选 version_id、可选简短 reason | 固定版本并更新 checkpoint 活跃引用；不运行脚本 |
| read_skill_resource | skill_id、相对 path、offset、limit | 读取该活跃版本附件，返回来源与分页；不读任意本机文件 |
| unload_skill | skill_id | 移除后续请求中的方法指导；保留历史，不自动终止资源 |
| run_skill_script | skill_id、相对 path/入口别名、argv 列表、可选 stdin_json、等待/截止参数 | 校验并运行版本内脚本，返回既有 ToolResult/job/resource 引用 |

V1 不为每个 Skill、每个脚本自动创建一个模型 Tool，避免工具 schema 随技能库膨胀。结构化参数只在入口声明 schema 时校验；没有 schema 的脚本允许 argv/标准输入，避免强迫所有第三方脚本重写。

load_skill 返回短确认、版本与资源目录，正文由下一模型请求的 context builder 作为有来源标记的方法区块完整注入。正文不同时完整写在 ToolMessage 和每轮系统区块，避免双份占用。

加载与依赖它的执行不能处于同一并行工具批次。load/unload 等图状态写入串行处理；稳定版本引用的只读附件读取可并行。脚本执行在下一轮才看到已提交的活跃引用。

重复 load 同一版本是幂等成功，不重复注入、不同于重复执行脚本；load 不存在的 Skill 返回结构化错误与检索建议。不要诱导模型扫描整盘找替代路径。

## 8. 上下文、压缩与恢复

### 8.1 唯一权威归属

checkpoint 增加有界的 active_skills 引用，记录 skill_id、version_id、package_hash、入口正文工件引用和加载来源；不保存整包正文或无限调用日志。

run.config_snapshot 保存 catalog_snapshot_id 和模块策略版本。安装配置、不可变版本元数据属于业务表；真实脚本执行属于 Executor 台账；具体正文和附件属于不可变工件。UI 仅投影这些数据。

### 8.2 每次实际请求重新装配

ContextBuilder 根据 active_skills 读取固定版本正文，放在用户目标、任务约束与可信运行规则之后。Skill 的用户编写指导与目标响应资料用不同来源标签呈现；外部响应不得伪装成 Skill 或覆盖可信规则。

技能正文与任务态势、工具 schema 一起进入最终 estimate_request 和 context manifest。记录“已加载”与“实际送入模型”分别成立；manifest 包含真正呈现的版本、正文字节哈希、资源片段及估计 tokens。

正文若超出可用预算，返回明确 skill_context_budget_exceeded，建议拆分参考文件或卸载无关 Skill；不静默截掉中间规则，不假称全部生效。可配置活动指导预算，建议初始 12000 tokens；这是上下文资源边界，不是任务时长、费用或尝试总数上限。

默认维持一个主 Skill，可组合少量辅助 Skill；不设总安装数量限制。第一版不自动按 LRU 丢弃仍活跃指导；模型或用户明确卸载，后续可引入有事件记录的失活建议。

### 8.3 压缩与恢复语义

- 压缩只处理会话历史；活跃 Skill 从版本引用重建，正文不会只剩模型摘要。
- 非活跃 Skill 的旧指导消息可以作为历史归档，不继续具有当前方法指导作用。
- 参考文件阅读历史可以压缩，正文中的稳定路径可再次解析。
- Runner 重启、主动分段、暂停继续均恢复同一 package_hash；源目录此时即使改动也不替换运行版本。
- 版本工件缺失或损坏时明确失败，不回退到 latest。
- state schema 提升并提供从当前 v4 的迁移；旧 checkpoint 默认 active_skills=[]，保留消息 ID、pending writes 与操作身份。
- load 的内容解析以 run 固定目录为依据，checkpoint 重放得到相同结果。

## 9. Windows 控制端与 Kali Executor 的脚本运行

### 9.1 首次使用才同步

纯说明和参考资料在控制端读取；无需为每个 Skill 提前复制全部文件到 Kali。首次执行某版本脚本时，通过认证执行端通道传输完整不可变包，按 package_hash 幂等缓存。

执行端接收临时包 → 校验文件清单与每文件哈希 → 原子发布缓存 → 在会话工作区 `.rexak/skills/<package_hash>/` 物化完整目录。不能只传一个 .py 文件，避免相对 import、模板和相邻数据文件失效。

只读是存储和文件工具策略，不宣称等价于可信 shell 下的内核沙箱。读取/执行前校验 manifest；发现包内容变化时拒绝并报告，不把哈希校验描述为网络隔离。既有 network_isolation=unavailable 继续如实显示。

### 9.2 复用执行底座

run_skill_script 由服务端解析已固定版本的脚本路径和解释器，使用现有 run_execution_kernel/command runtime 以 argv 执行，不让模型拼绝对路径或把参数拼入 shell。

默认工作目录是本次产物目录；脚本文件仍位于完整包目录，保留正常 __file__、导入和相邻资源语义。提供环境变量：

```text
SKILL_DIR=<执行岛上当前版本的完整目录>
SKILL_OUTPUT_DIR=<当前逻辑操作的工作区产物目录>
WORKBENCH_WORKSPACE=<当前会话工作区>
```

输出目录由稳定 operation_id 派生，重放复用。同一工具调用及相同有效参数复用原操作；显式重测用新的逻辑操作身份。request_digest 包含 package_hash、script relative path、解释器、argv/stdin、cwd 和影响执行的环境参数。

为兼容普通脚本，支持 argv 和可选 JSON 标准输入，不强制脚本调用 Rexak SDK。默认支持 Python/shell/Node，以执行端实际能力为准；其他解释器明确报告未支持或通过将来显式扩展提供。

短等待、background、执行截止、输出分页、取消、watch 和跨重启核对全部复用现有执行机制。Skill 不拥有另一份 job 状态。传输失败不启动；派发后状态不明保留 unknown，不自动运行第二次。

### 9.3 依赖

compatibility 是说明；rexak.yaml/requirements.txt/package.json 可辅助预检。仅检查已声明解释器、二进制和锁定依赖，不 import 用户模块来执行自检。

缺依赖时正文仍可读取，受影响入口标记 unavailable，返回缺失清单。V1 不在导入或加载时执行 setup/install hook。常用基础依赖由执行岛维护；任务级依赖复用现有 trusted 命令和隔离依赖目录，安装过程保留日志，不修改系统 Python。

一个正文可读、脚本缺依赖的 Skill 显示“部分可用”，不是整体从目录消失。依赖就绪和 Skill 效果验证分别记录。

## 10. 工具依赖与 MCP 扩展

第一版使用现有 HTTP、浏览器、文件、命令和任务工具。Skill 声明需要什么，由宿主检查实际提供什么；声明本身不扩大能力。

允许工具集合遵循宿主授权、任务模式和执行端能力；Skill 只选择已授权能力。纯方法 Skill 无需声明 allowed-tools。多个 Skill 加载不会把其他 Skill 的工具权限合并成更高权限，也不全局交叉限制既有基础工具。

为未来 MCP 保留 `SkillToolResolver` 接缝：宿主管理集成、连接配置和密钥，Skill 引用已配置 integration_id；加载后仅披露被批准且实际可用的工具。恢复根据 checkpoint 活跃版本重建披露，不靠扫描压缩后的自然语言猜测。

MCP 连接管理、凭据和厂商专用工具映射不纳入 V1 的“已支持”范围。外部 Skill 写了 mcps 并不会自动联网启动服务；页面必须明确兼容性限制。后续接入后也不把 Skill 加载视为新增服务授权。

## 11. 产物、证据和归因

Skill 脚本产生的文件先作为工件登记，保留 package_hash、相对脚本路径、operation_id、身份来源和时间。脚本 stdout 或 JSON 中自称 confirmed 不影响业务状态。

脚本能输出普通文件/JSON。可选的产物 schema 只帮助发现文件与生成 candidate 建议；Observation/Lead/Finding 的语义判断仍由主 Agent 和现有领域服务处理，正式确认仍由验证记录支撑。

常规 HTTP/浏览器工具调用也能关联当前 Skill，但区分 explicit（run_skill_script 明确指定）与 contextual（调用时有活跃指导）；多个 Skill 活跃时不猜测唯一贡献者。工具的真实 step/lead/identity 来源仍按原机制保存。

## 12. 管理 API、CLI 与界面

### 12.1 用户管理

新增“技能库”页面：目录与 ZIP 导入、刷新、名称/描述/来源、启停、项目可见范围、发布版本、兼容诊断、依赖状态、只读预览、导出和历史使用。

用户已配置根目录后，直接编辑文件并刷新即可生成新版本，不要求每次改包手动重启服务或编译代码。新增用户自编 Skill 校验通过即可按根目录默认策略启用；项目限制单独配置。

目录和 ZIP 导入共用同一发布管线。写操作使用现有认证/CSRF。Agent 可以在任务工作区提出 Skill 草稿；只有用户管理入口负责安装发布，避免任意目标资料自动成为方法指导。

聊天支持技能选择与当前活跃版本展示。活动流展示加载、读取附件、脚本开始/结束和失败原因；加载不是后台任务运行中的动画，避免状态混淆。

### 12.2 拟议 API

```text
GET  /api/skills
POST /api/skills/import
POST /api/skills/rescan
GET  /api/skills/{skill_id}
GET  /api/skills/{skill_id}/versions
PATCH /api/skills/{skill_id}              # 启停、显示信息与范围配置，不编辑已发布正文
GET  /api/skills/{skill_id}/versions/{version_id}/files
GET  /api/sessions/{session_id}/skills    # checkpoint 活跃引用的只读投影
```

导入本机目录只接受已配置根目录中的路径。更新内容可编辑源文件后刷新或导入一个新包。界面若支持在线编辑，保存为新草稿并走同一发布管线。

CLI 提供 workbench skills list/validate/import/rescan/export。名称为设计建议，实施时按现有 CLI 结构确认。前端类型从 OpenAPI 生成。

## 13. 数据模型与代码落点

### 13.1 最小新增持久结构

1. skill_installations：稳定 ID、owner、来源配置、启用状态、当前版本指针；项目绑定可用单独关联表，不能把整个正文复制到项目配置。
2. skill_versions：安装 ID、不可变 package_hash、包/manifest/入口工件引用、解析元数据、创建时间和兼容诊断版本。
3. catalog snapshot：存为现有工件，由 run.config_snapshot 引用；索引可派生重建。

使用日志走已有 tool_operations/events/spans/context manifests；V1 不增加一个拥有当前活跃状态的 skill_sessions 表。若未来增加统计表，只作为可重建投影。

### 13.2 建议目录

```text
src/agent_workbench/
  contracts/skills.py
  skills/
    parser.py          # SKILL.md 与可选扩展解析
    package.py         # 路径、文件清单、内容哈希与包校验
    registry.py        # 目录发现、发布版本和可见性
    service.py         # 加载引用、附件读取、兼容诊断
    search.py          # 中文可用的检索和分页
  tools/skills.py
  agent/middleware/skills.py
  api/skills.py
executor/src/workbench_executor/
  skill_packages.py    # 接收、校验、缓存和工作区物化
```

修改现有 factory、registry、context_builder、compaction 请求预算接缝、state/state_migration、Runner 快照、execution contracts/client、事件枚举、run bundle 与前端。

SkillsMiddleware 的职责限制为引用状态/可见目录的准备；正文装配进入现有 ContextBuilder。不能新增独立 awrap_model_call 再次调用模型，不改变已锁定的 ResponseRecovery → ContextBuilder → ModelObserver 次序。压缩所用预算视图也必须包含活跃 Skill，避免主请求和摘要触发使用两套不同预算。

## 14. 从 search_methods 平滑迁移

现有 methods/*.md 转为 builtin Skill，每份保留原内容、原方法名与原版本来源。因为它们没有标准 frontmatter，首次迁移通过显式 adapter 构造元数据，不要求用户手动修复历史正文。

search_methods 保留兼容入口，旧调用与 operation identity 不变；新运行推荐 search_skills/load_skill。旧 search_methods 历史仍按“读取资料”解释，不追溯伪造 active_skills 或使用事件。

旧任务恢复不强制加载新增 Skill。需要采用新机制时在新指令边界显式加载。内置“硬约束”继续由系统和代码维护，不因禁用某个方法 Skill 而消失。

## 15. 错误处理与可观测性

建议结构化错误：skill_not_found、skill_ambiguous、skill_disabled、skill_version_missing、skill_package_invalid、skill_hash_mismatch、skill_resource_not_found、skill_path_denied、skill_dependency_unavailable、skill_context_budget_exceeded、skill_not_active、skill_extension_unsupported。

输出明确操作失败的阶段和纠正方式；不把这些错误记作目标阴性结论。加载失败不执行脚本，不吞异常后返回空正文。

记录 skill.catalog_selected、skill.loaded、skill.resource_read、skill.unloaded、skill.execution_requested 等事件及版本；是否真正呈现给模型以 context manifest 为准。实际脚本开始/结束以 Executor 台账为准，事件用于解释而非重建第二份执行权威。

记录加载次数、未命中、依赖失败、正文/附件 token 估计和后续关联操作。避免“加载即使用有效”“调用越多越好”这类指标。现有主模型用量台账继续计费，Skill 本身不偷偷增加一个分类模型。

导入 ZIP 检查解压路径、绝对路径/盘符、软/硬链接、重复路径、大小与文件数、Windows 大小写冲突；只提取普通目录/文件。不导入 .git、node_modules、venv 与运行结果等不必要目录。限值在配置中明确，不能无限展开压缩包。

文本使用 UTF-8 无 BOM；二进制 assets 保留原字节。文本无效时明确指出文件，不用 GBK/Latin-1 兜底。发布刷新遇到文件在读取期间变化时重试或返回冲突，不发布混合快照。

## 16. 实施阶段与退出条件

| 阶段 | 交付 | 退出条件 |
| --- | --- | --- |
| A | 通用格式、解析校验、用户目录/ZIP、不可变发布与目录检索 | 添加用户 Skill 无须修改代码；有完整错误与版本记录 |
| B | 五个 Agent 工具、活跃引用、显式/自主触发、预算和恢复、旧 methods 兼容 | 官方 create_agent 合成闭环可加载、压缩、暂停恢复；请求中实际出现正确版本 |
| C | 执行岛包同步、完整资源目录、脚本执行、依赖预检、产物归因 | 端到端通过原执行台账运行，无重复副作用；控制端不执行脚本 |
| D | 技能库页面、项目可见性、聊天选择、更新/回退、事件、运行包导出 | 用户可完成导入—使用—更新—历史复核，无终端必做步骤 |
| E | 本机与 Kali 部署验收及真实效果实验准备 | 原桌面入口可用；真实模型效果由用户运行并另行评价 |

A–D 是第一版完整交付，不把只有读取 SKILL.md 的半成品标为完整 Skill 模块。E 按仓库部署约定核对活动任务、安装哈希、能力与原桌面启动入口，保留已有任务、凭据、资源与暂停状态。

V2 按实际需要增加 MCP 工具 resolver、Git 来源更新、技能关联推荐和更复杂依赖环境；第一版不建设公开市场、自动技能生成或独立多 Agent 调度。

## 17. 必须覆盖的验收场景

| 类别 | 验收内容 |
| --- | --- |
| 格式 | 最小包、中文描述、可选目录、未知扩展、无效 YAML、BOM/编码错误 |
| 包与版本 | 同内容复用；正文/附件/脚本变化均产生新 hash；刷新中途变更拒绝；ZIP 越界拒绝 |
| 发现 | 用户明确指定；模型匹配；不适用样本不误用；大量 Skill 可搜索分页，无静默隐藏 |
| 加载 | 幂等 load、同名歧义、状态写入串行、加载与执行同批拒绝、未启用能力不会被授予 |
| 上下文 | 正文只完整呈现一份；实际发送 manifest；超预算不截断；只读参考片段有来源 |
| 恢复 | 压缩、分段、Runner 重启、暂停继续保持原 hash；旧源更新不污染；旧 v4 状态迁移 |
| 执行 | 缓存命中不重复传输；完整包相对 import/资源可用；argv/stdin 不拼 shell；参数正确 |
| 不确定性 | 包同步失败不执行；派发回执丢失按 operation_id 核对；unknown 不自动重跑 |
| 生命周期 | 卸载指导不冒充已取消进程；旧包引用保留；执行端重启沿现有 job/PTY 语义 |
| 业务边界 | 脚本自称成功不写 confirmed；示例数据不成为本次证据；原 finish gate 仍生效 |
| 用户操作 | 复制目录/ZIP 导入、刷新、启停、项目绑定、版本切换、导出与历史预览 |
| 兼容 | search_methods 旧调用可恢复；已有幂等回执不变化；未知产品专用能力明确显示限制 |

工程回归证明机制正确；真实模型另测触发准确度、附件选择、脚本有效使用与任务收益。对比固定模型/指令/环境并保留人工介入，分别统计“可发现、已加载、实际呈现、关联执行、形成有效结果”。不把 Skill 加载次数或测试通过数当成测试能力提升证明。

## 18. 第一版默认决策清单

- 格式：通用 SKILL.md；无必需 Rexak 私有文件。
- 来源：内置包和用户显式配置根目录；目录与 ZIP 共用发布管线。
- 选择：用户显式指定 + 主 Agent 按描述选择；不另设分类模型。
- 内容：目录、正文、附件分层加载；正文按活跃版本每轮重建。
- 版本：整个包内容寻址；run 目录快照与 session 活跃版本分别固定。
- 执行：首次使用同步完整包到 Kali；复用 command/script runtime 与 operation_id。
- 权限：延续宿主、任务和 trusted 执行策略；Skill 不授予新权限，不额外逐条审批。
- 数据：checkpoint 管活跃引用，业务表管安装/版本，工件管内容，Executor 管执行。
- 更新：刷新生成新版本，默认下一 run 可见；已有活跃版本显式升级。
- 管理：技能库页面 + CLI；后续添加 Skill 不改主循环或 Python 注册表。
- 迁移：现有方法资料成为 builtin Skill；search_methods 兼容保留。
- 范围：第一版完成纯说明、附件和脚本三种能力；MCP 等高级集成作为后续明确扩展。

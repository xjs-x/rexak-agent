# Skill 模块工程与部署验收

日期：2026-10-17。工作区 `C:\xjs\rexak-agent`，分支 `codex/implementation-v1`，基线提交 `3f6d258`。本轮保留已有未提交修复及运行数据，不修改旧项目 `C:\xjs\xak-agent`。

## 交付范围

- 标准 `SKILL.md`、可选附件与 `rexak.yaml`，目录/ZIP 发布、完整包内容哈希、不可变版本、固定运行目录。
- 五个 Agent 工具和渐进加载；实际模型上下文 manifest、正文预算、活跃引用恢复、显式用户选择和项目默认。
- 官方 `create_agent` 保持唯一主循环；状态 v4→v5 显式迁移，卸载清除引用，选择消费标记保持有界。
- Kali 完整包缓存、相对 import/资源、配置解释器、依赖预检、argv/JSON stdin、稳定操作身份及原执行台账。
- 技能库页面、管理 API/CLI、启停、项目可见性、版本预览/回退和 ZIP 导出。
- Skill 活动事件、实际呈现版本、普通操作的指导关联与脚本明确归因；私有实验包包含固定目录和完整版本包。

使用方法和第一版边界见[Skill 说明](../../../skills.md)，设计依据见[原方案](../../../plans/skill-module-design.md)。没有新增具体渗透 Skill 内容；内置方法由原资料 adapter 提供。

## 本机验证

完整回归命令：

```powershell
uv run pytest tests/unit tests/integration tests/recovery executor/tests -q -m 'not real_model and not real_executor' --tb=short
```

最终完整回归：**709 passed、60 skipped、1 deselected**，用时 216.13 秒；原始记录为 `data/deploy/skills-2026-10-17/regression.txt`。另外运行过固定 Skill 依赖导出、旧库兼容和操作重放组合测试，以及入口别名、JSON stdin 和指导/执行归因检查；它们与完整回归重叠，不将数量相加。

Ruff、Pyright、UTF-8 无 BOM 检查、执行端协议生成一致性、前端 OpenAPI 类型生成及生产构建均通过。依赖已通过 `uv sync --frozen --all-packages --group dev` 同步。

重点机制证据：

| 场景 | 实际验证 |
| --- | --- |
| 正文呈现 | 官方 `create_agent` 合成模型捕获请求；首次只给目录，load 后下一请求正文完整且只出现一份 |
| 恢复和更新 | 同一 checkpoint 重建图后保留原包；新发布正文不替换旧活跃版本 |
| 显式卸载 | 已消费的旧用户选择不重新激活，checkpoint 不积累卸载 tombstone |
| 预算 | 超限正文在激活前拒绝；大文件清单可分页，不把整包目录无限塞进 ToolMessage |
| 状态迁移 | 消息、计划、待执行节点保持；迁移不执行工具，续跑只执行一次 |
| 发布和授权 | 内容复用、附件变化改 hash、损坏包拒绝、CSRF、项目限制和停用后拒绝运行 |
| 操作身份 | 固定版本经原执行内核派发；重复 tool_call 复用同一操作和资源，argv 中空格及特殊字符不拼 shell |
| 能力边界 | 关闭 command 选择后移除脚本入口，并提供派发拒绝原因；资料检索和加载仍可用 |
| 历史复盘 | 私有包导出固定目录中的旧版本和完整 ZIP，新发布版本不替代历史依赖；包校验通过 |

Windows 上跳过的 60 项涉及 Linux 进程/PTY、符号链接权限和独立目标 fixture，不计为通过。真实模型测试排除；这些工程测试不验证模型触发准确度或渗透效果。

## 浏览器验证

运行：

```powershell
uv run python scripts/accept_skills_ui.py --output data/deploy/skills-2026-10-17/ui
```

实际 React SkillLibrary 组件在 Edge 无头浏览器中通过 9 类交互检查：中文检索、显式选择、正文与附件预览、版本切换、停用后取消选择、项目默认、刷新诊断、目录与 ZIP 导入、无浏览器脚本异常。API 为独立 fixture，不连接日常任务或模型。

证据：`ui/ui-results.json`、`ui/skill-library.png`；截图已检查，无正文和按钮溢出。

## 数据与本机入口

更新前确认日常数据库活动运行租约为 0，本机没有原 API/Runner 进程；Kali 部署脚本另检查活动执行任务，保留已有凭据、台账、任务和暂停状态。

更新后日常 run 各状态数量与更新前完全一致，活动租约仍为 0；记录见 `data/deploy/skills-2026-10-17/preserved-state.json`。

数据库和工件备份位于 `data/backups/skills-2026-10-17/20261017T020916950194Z/`，`verify-backup` 校验通过，包含 3269 个文件。已将业务库前向迁移至 `f7a2c4e6b901`；没有重置历史 run 或改写旧 checkpoint。

已发布 4 个现有 builtin 方法 Skill，个人根目录为 `C:\xjs\rexak-agent\data\skills\user`。

原桌面快捷方式 `C:\Users\HP\Desktop\Rexak Agent.lnk` 的目标与工作目录已核对，指向本仓库 `start-workbench.cmd`。按同一启动链运行：

```powershell
start-workbench.cmd --api-only --no-browser --smoke-seconds 55
```

数据库、SSH、Executor 和 doctor 检查通过；本机 `/health`、`/console/`、Skill 列表、详情和固定版本资源接口均返回 HTTP 200。该模式没有启动 Runner，未消费历史排队任务；冒烟结束只停止本次创建的 API。证据为 `data/deploy/skills-2026-10-17/launcher-api.json` 及 `data/logs/launcher/` 日志。

## Kali 部署和实际执行

已构建并离线安装 `workbench-executor` wheel、同步源文件、重启服务、核对 23 个安装文件哈希、启动实例变化、身份、协议及 pip 依赖。能力声明包含 `skill_packages=available`，保持 `execution_mode=trusted`、`network_isolation=unavailable`。

无害脚本冒烟：

```powershell
uv run python scripts/accept_skills.py --output data/deploy/skills-2026-10-17/executor-smoke.json
```

8 项检查通过：能力声明、完整包相对导入、中文资源、独立产物目录、argv 原样传递、缓存复用、同一操作复用资源且只执行一次、缺依赖明确拒绝。只在专属验收 session 中读取自有 fixture 和写入本地计数文件，没有访问目标或调用模型。验收进程已以退出码 0 结束，未关闭用户资源。

部署证据：`data/deploy/skills-2026-10-17/deployment.json`、`deployment.log`、`executor-smoke.json`。这些文件保存在运行数据目录，未将私有运行原件加入源码。

## 明确边界

第一版使用一个配置根目录；不提供 Skill 市场、Git 自动更新、自动依赖安装、MCP 适配、JSON Schema 参数校验或正文内容生成。产品专用字段显示诊断，不转换成权限。产物目录按需读取，不自动无限上传所有产物。

工程与部署验证通过不代表实际安全测试效果已提高。真实模型的触发准确度、方法遵循、脚本利用和最终任务收益仍由用户实验确认。

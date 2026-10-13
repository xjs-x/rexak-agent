# V1 本地工程与试验操作

代码实现与真实效果验收分别登记。当前工程检查使用脚本模型、捕获请求和隔离 PostgreSQL；不能据此声称已完成靶场目标或优于 B0。

## 启用代码

现有工作库新增字段均可空：线索别名、操作派发时焦点、领域工具重放回执、报告捕获快照。历史字段保持未知。API/Runner 会拒绝不匹配的业务 schema，不在启动时自动迁移。

先按原备份入口保存数据库和工件，再显式运行：

```powershell
Set-Location 'C:\xjs\rexak-agent'
uv run workbench db-init
```

本次编码过程只迁移了隔离测试库，没有修改日常工作库，也没有启动真实 Agent。启用新代码需要正常重启 API/Runner；执行岛入口不变。

图状态升级为 v2。Runner 通过官方 `aupdate_state` 显式适配 v1：保留消息及 ID、计划、摘要来源和待执行节点，从领域表重新投影活跃线索及后台任务。无法识别的版本、边界变化或含未提交并行结果的旧 checkpoint 拒绝自动恢复；先按原 operation/job 台账核对，不能直接重放工具。适配不是改版本数字或修改框架序列化表。

## 准备 B0 与 V1

B0 固定为 `a37ae3786ab495035bbe14bd50276a7553438995`。使用该 commit 的独立 checkout 运行原代码。V1 使用完成本地验收的冻结新 commit；准备命令拒绝源码未提交或仍指向 B0 的 V1。`.workbuddy` 与本地 `.env` 不进入源码指纹。

复制 [trial-template.json](trial-template.json) 到本机私有目录，填写 trial ID、分组、环境别名、重复编号和已知初始快照。该文件没有目标答案。缺失信息保持 `null`，不猜填。四个环境用 T01–T04；先各组各环境一次检查流程，再追加重复，版本变化必须另记。

从 V1 checkout 执行准备命令，`--source-root` 指向本次分组的冻结代码：

```powershell
uv run workbench prepare-trial 'C:\private-trials\T01-V1-1-input.json' --source-root 'C:\xjs\rexak-agent' --output 'C:\private-trials\T01-V1-1'
```

生成目录内有登记、源码指纹、`environment.json`、`setup.json` 和 `load-environment.ps1`。它只生成配置，不创建数据库、启动模型或访问目标。输出已存在时拒绝覆盖。配置含本机数据库连接信息，整个目录带 `.gitignore`，留本机私有目录。

按 `setup.json` 的 SQL 创建该 trial 的独立空库。在启动 API、Runner 的每个 PowerShell 窗口加载配置，再在对应分组源码目录初始化和启动：

```powershell
. 'C:\private-trials\T01-V1-1\load-environment.ps1'
uv run workbench db-init
uv run workbench api
```

Runner 在另一个已加载同样配置的窗口执行 `uv run workbench runner`。B0 使用原 commit 的入口和迁移，V1 使用新入口；不能让不同代码共用已迁移的工作库。模型、账号权限、工具能力和初始目标状态按方案保持可比，由操作者登记。

每个 trial 都有独立数据库、数据根与 owner。V1 还同时隔离项目和方法经验的读写分支；B0 原代码不识别分支参数，但独立 owner 仍隔离其旧命名空间。冷启动默认没有其他 trial 经验。跨环境方法迁移另做独立试验，明确登记读取快照，不能混入本目标答案。

任务没有新增时间或费用硬上限。暂停、人工提示、手工继续和观察截止由操作者如实记入私有笔记与 `end_kind / assisted_from`；不要把辅助后的结果当作纯自主完成。

## 观测、报告与封包

正常可见历史归档可用 `search_history` 分页和 `read_artifact` 展开。它不检索供应商隐藏推理。焦点、计划理由、恢复说明和证据来源可在只读计划页查看；操作详情显示派发时关联，不证明操作有助于该问题。

关键观察、新发现和已完成子目标触发阶段报告；暂停或终态也生成报告。报告不要求存在 confirmed，不自动结束任务。失败保留原快照与版本，可重复触发同一阶段重试；历史 confirmed 引用已经失效时不强行发布。普通工具结果、模型归档或报告渲染不作为测试知识进展。

用量台账失败产生待同步终态时，只补齐已有尝试，不调用模型：

```powershell
uv run workbench sync-model-observations
```

试验后补齐私有登记中的实际 session/run 集合和时间，再导出全部续聊：

```powershell
uv run workbench export-run <RUN_ID> --bundle --trial 'C:\private-trials\T01-V1-1\trial.json' --output 'C:\private-trials\exports'
uv run workbench verify-bundle 'C:\private-trials\exports\<RUN_ID>\bundle'
```

B0 可由 V1 导出器直接只读连接其原始数据库：另开窗口加载 B0 的环境配置，但在 V1 checkout 执行导出；不执行迁移、不启动 V1 Runner。旧 schema 缺失的新字段标 `null`，旧 manifest、阶段快照和来源指纹仍标缺失。导出机源码不会冒充历史运行源码。

包内有事件时间线、领域与执行台账、各 phase 模型调用、真实可用 manifest、已记录阶段快照、报告与证据原件及哈希索引。数据库、checkpoint 和文件读取不是统一原子快照。manifest 状态是网络尝试前准备，不证明网络成功；召回展示不证明模型已复评。Runner 接管时记录的是当时源码文件，不冒充 run 创建时或已加载字节码。

封包内人工笔记和私有评价是空白占位；后续记录另存 sidecar，不要修改已封包文件。原件可能含敏感自由文本，只留本机。哈希检查一致性，不判断结论正确。

## 离线复盘

```powershell
uv run workbench analyze-bundle 'C:\private-trials\exports\<RUN_ID>\bundle' --output 'C:\private-trials\analysis\T01-V1-1.json'
```

可传多个包，按 trial 逐次保留。输出包括 phase 调用、已知和未知 tokens/费用、币种、估计误差、片段与暂停事件、召回来源、重复请求候选和报告版本。输入包损坏或试验 ID 重复会拒绝分析。

实际结果、结论准确性与全站召回率保持未知，需操作者的私有预登记项、原始工件与关键转折复核。重复请求可能是有意复查；不能自动判无效。不同观察长度、提前停止、基础设施失败和人工辅助不能悄悄从比较表删除，也不直接平均完成时间。

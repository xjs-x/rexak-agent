# 外部 Skill 边界恢复验收

本次仅恢复外部 Skill 边界，保留后续合同计划、证据、profile 和 checkpoint fencing 修改。

## 结果

- 全量单元及集成：724 passed，3 skipped。跳过项包含 Windows 权限限制及未配置真实模型；没有真实模型或目标效果实验。
- 独立审计 24 passed，主探针 8 passed；旧审计包含在全量集成回归中。
- Ruff、Pyright、UTF-8 无 BOM 检查和前端生产构建通过。
- 真实 React 组件隔离浏览器演练通过，覆盖空库存、显式导入、中文检索、固定版本、附件、停用与项目绑定。
- 原桌面 `start-workbench.cmd --api-only --no-browser --smoke-seconds 65` 启动链通过；实际服务的静态资产与新构建字节一致。
- 日常库 GET 技能列表为 0；显式刷新为 0，错误为空；再次 GET 仍为 0。四个旧 builtin 均继续停用，四份历史版本保留，不执行真实数据修复。
- 前后 46 张受保护表指纹一致，凭据哈希、任务历史、计划及 Phineas 暂停状态一致。模型调用数保持 826。
- Kali 只读身份及能力检查通过，skill_packages 可用，network_isolation 仍明确不可用。本次没有修改 Executor 源码或发起目标操作。

原始证据位于本机忽略目录 `data/acceptance/2026-10-19/skill-regression/`：`regression.log`、`ui/ui-results.json`、`http-after.json`、`live-before.json`、`live-after.json`、`before.json`、`after.json` 及静态检查日志。目录含个人运行数据，不提交。

## 防回归

`test_external_skills_boundary.py` 验证 GET 不隐式导入、显式刷新两次不生成宿主方法、空库存不改 run/工件、旧 builtin 即使数据库误启用也不可发现或执行，以及旧 hook 恢复不重放工具。

`test_external_skill_assembly.py` 验证默认工具和中间件不启用 Skill，原 search_methods 存在，外部扩展仍受命令能力门禁约束。

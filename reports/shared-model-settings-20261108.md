# Main / Pi Worker 共用模型设置交付（2026-11-08）

## 行为与配置归属

前端「模型设置」是 Main 与 Pi Worker 共用的唯一设置入口。保存默认模型、Base URL 或 Key 后无需再次执行 Worker 配置命令，也无需为刷新配置重启。Worker 每次启动任务时，通过鉴权回环接口取得父 Run 的固定模型绑定与当前共用 Key；已有 Main/Worker 任务不在执行途中切换模型。暂停恢复沿用主任务模型并读取更新后的 Key。

本机历史 `ultra-worker-model.json` 保留但不再读取。`--configure-model` 兼容入口改为设置共用默认模型，不再复制密钥；显式 `--mock` 保留。服务健康与模型配置可用性分别报告；缺 Key 时控制台仍可进入修复，Worker 任务明确失败，不回退旧 Key 或 Mock。

## 实现位置

- `src/agent_workbench/config/worker_model.py`：从 Main 的模型档案、Run 绑定和本机密钥库派生配置；移除第二份配置的运行读取/写入。
- `src/agent_workbench/api/worker_model.py`：内部服务令牌与回环来源校验，委派所属 Session/Run、租约/控制版本/撤销守卫；响应禁止缓存，不出现在浏览器 OpenAPI。
- `worker/src/shared-model.ts`、`server.ts`、`executor.ts`：每任务读取、固定本任务配置、真实模型指标归属；失败不启动模型/工具，取消与失锁继续受原生命周期管理。
- `scripts/start_ultra_worker.py`：原桌面入口使用共享模式，模型 Key 不再作为启动环境快照传递。
- `web/src/ModelConfiguration.tsx`、`api/routes.py`：明确共用范围、默认模型开关及在途任务行为。
- `docs/decisions/0026-ultra-shared-model-settings.md`：按用户要求替代 decision 0020 的独立模型快照约定；README 与部署说明同步更新。

密钥只通过可信本机通道到 Node 运行内存，不进入 A2A 任务包、Pi 消息、结果工件或 Kali。不将配置健康检查声称为供应商鉴权或解题验证。

## 验证

|检查|结果|
|---|---|
|本机依赖 `uv sync --frozen --all-packages --group dev`|111 包核对通过|
|配置/绑定/更新 Key 单元测试与新增 API 集成|10 passed；使用隔离 PostgreSQL 与合成密钥|
|新增 API + 全 Worker Node 回归|3 passed；Node 子测试使用同一隔离测试库，无真实供应商调用|
|Worker TypeScript 与构建|通过|
|前端 TypeScript/Vite 构建|通过；保留已有 chunk 大小提示|
|本次修改 Python 文件 Ruff / Pyright|通过，0 errors|
|默认源码 UTF-8 检查|790 文件通过；相关文件与文档另行定向检查通过|
|全仓 Ruff|未通过，160 项；完整输出 work/shared-model-ruff-all.log，未批量修改历史实验/其他工作|
|全仓 Pyright|未通过，8 项，均位于既有 test_execution_fence_coalescing.py 与 test_worker_progress.py；本次修改文件没有类型错误|
|完整 unit/integration|用户要求停止后已终止，不再运行；退出前输出到95%并出现2项失败标记，未形成完整结果，不算通过，也未据此推断失败归属。以已通过专项验收为准；日志 work/shared-model-regression.log|

完整静态检查不通过与本次专项检查通过分开记录，不将局部通过冒充全仓通过。没有 Git commit/push，没有连接真实靶场或调用付费模型。

## 个人部署验收

部署前确认控制端活动 Run 与 Worker 均为0；历史暂停/恢复任务保留。Kali identity kali-01、service0.1.0、protocol1、capabilities2，boot `3485002-2470199d96ea`；27个实际安装 Python 文件与本机逐项哈希一致，active_jobs=0。本次执行端代码未变，因此保留该实例，不重启 Kali。

Windows API/Runner/Worker 已通过原 `start-workbench.cmd --no-browser --no-recover` 重载，新进程信息见 `work/shared-model-stack-after.json`。停止时先核对项目专属控制台并发送 Ctrl+Break，服务进程全部退出；原 cmd 因脚本 pause 留存，确认其无子进程后只关闭该空启动壳。没有恢复旧任务。

实际 HTTP 验收：API health 与 console 正常；页面及 JS/CSS 与当前构建逐字节相同。鉴权内部配置响应与 Main 的模型档案、Base URL、当前密钥库值完全相同（只记录布尔结果，不保存响应密钥）；未鉴权请求401，配置响应no-store。Worker health 实际返回 `model_source=controller_shared`、`model_configured=true`、`provider=bailian`、`model=deepseek-v4.1-flash`。A2A身份检查通过。配置读取端到端已验收，不代表供应商真实认证成功；未启动新CTF/真实模型调用。

`work/shared-model-live-acceptance.json` 保存最终无密钥验收结果。原模型偏好、共用密钥库、历史独立Worker快照及其他凭据文件SHA256前后一致；Session/Run/Artifact数量一致，14个历史非终态Run的状态和updated_at逐项一致；迁移保持f2d91a4c8b77。Kali旧boot保留。原取证报告与真实执行原件未改写；复用只读预检脚本时更新了一份 `work/latest-pwn-live-state.json` 状态快照，本次副本另外保存为 `work/shared-model-preflight.json`。

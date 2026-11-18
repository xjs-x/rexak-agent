# 测试与验收

验收日期为 2026-11-18。控制台使用原应用的 API 启动命令、回环随机端口、独立 PostgreSQL 测试库、临时数据目录及临时模型密钥库。模型服务为本机 HTTP Mock。浏览器使用 Windows Edge，检查了 1280 × 900 和 390 × 844 两种视口。

| 检查 | 结果 |
| --- | --- |
| 模型设置相关 Python 单元及集成测试 | 27 通过，含实际浏览器验收 |
| Web Vitest | 70 通过 |
| Worker TypeScript 和测试 | 类型检查通过，49 通过，12 跳过 |
| Web 生产构建 | 通过，Vite 提示主包超过 500 kB |
| Pyright、UTF-8 编码和本轮改动文件的 Ruff | 通过 |
| 工作区全量 Ruff | 未通过，包含 `_ctf` 与 `work` 等现存目录中的问题 |

API 测试验证了模型目录路径、Bearer 认证、当前草稿 Key、已保存 Key、独立目录 URL、百炼格式分页、超时、断连、去重、空目录、错误结构和体积限制。401、403、404、405 返回分类错误；Mock 的跨来源重定向目标没有收到请求。迁移测试确认旧地址、旧默认模型和旧 Key 保留，首写备份可读取，自定义模型能力与预算可重载。改址和删除后的历史绑定仍能从原槽取 Key；Main 序列化写入所选 Model ID 与请求预算，Worker 读取同一个固定绑定。

浏览器测试实际从左侧打开模型设置，验证字段错误、填写新 Provider、查询中的加载状态与取消、Escape 关闭后保留表单、搜索、全选与清空、键盘勾选、批量添加、能力编辑、设默认、保存和刷新重开。使用已保存 Key 再次查询时，同名型号显示已添加。模拟 404 后完成手动添加、重复项提示、已添加模型搜索与移除，并检查窄屏没有横向溢出。截图只使用测试配置。

| 界面 | 截图 |
| --- | --- |
| 提供商列表 | [01-providers.png](screenshots/01-providers.png) |
| 新增提供商 | [02-provider-editor.png](screenshots/02-provider-editor.png) |
| 目录选择 | [03-discovery-picker.png](screenshots/03-discovery-picker.png) |
| 能力与预算 | [04-model-capabilities.png](screenshots/04-model-capabilities.png) |
| 窄屏手动添加 | [05-narrow-editor.png](screenshots/05-narrow-editor.png) |

使用原百炼凭据单独查询真实目录一次，`GET /compatible-mode/v1/models` 返回 HTTP 200、184 个唯一型号，并包含 `deepseek-v4.1-flash-1017`。浏览器使用 Mock 返回的同一 ID 验证添加和默认模型保存。真实请求仅访问模型目录。

桌面快捷方式仍指向 `start-workbench.cmd`。本机模型设置及 DPAPI 文件的校验值验收前后相同。Kali 只读检查返回 `kali-docker-01`、协议版本 1、能力版本 2，已安装的 27 个 Python 源文件与本机对应文件哈希一致。生产入口未启动，生产服务未重启，验收没有访问或修改生产 Run。

2026-11-19 提交前执行完整 Python 回归，结果为 1,364 通过、6 失败、5 跳过。失败涉及地址规范化后的旧预期、地址错误提示及拆分设置 API 后的测试配置注入。已补充地址提示、更新旧用例并新增完整推理 URL 规范化验证。随后重跑模型相关 5 个测试文件，31 项全部通过。修正后未再次运行全量 Python 回归。前端构建与 70 项测试通过，Worker 构建与 49 项测试通过、12 项跳过。暂存 Python 文件 Ruff、Pyright 与编码检查通过；全量 Ruff 的 458 项问题来自历史工作目录等未纳入本次提交的文件。提交前再次只读核对 Kali 的 27 个 Executor 源文件，全部与本机哈希一致。

# TSecBench v1 接入准备记录

日期：2026-11-06。

## 结果与边界

推荐本地模式 + 官方 Python SDK；完整说明见
[接入说明](../docs/tsecbench-v1-local-evaluation.md)。
已安装并核对 SDK 0.1.2，实际调用路径为 `/openapi/v1/challenges`，
与官网当前接入说明一致。PyPI README 的部分 API 表格写作 `/api/v1`，
本次以实际发行代码和官网当前说明交叉核对，没有手写第二套 API。

已补上评测入口的 `--execution-mode standard|ultra`，默认保留 Standard。
选择会传给原 Session 创建 API，并写入评测记录；复用原 Agent/Runner，
不新增模型循环，不改 Executor、数据库结构或桌面入口。

本次没有连接旧平台任务、启动真实靶场、调用真实模型或提交 flag。
真实 VPN、任务凭证、当前题集、模型配置及单题端到端效果仍待后续核验。
当前入口仅单题，整套题集自动调度未实现，不能宣称 63 题全流程已就绪。

## 代码与检查

提交范围包含用户当前工作区的已跟踪产品代码、测试及文档改动，
并包含尚未跟踪的 `tools/artifact_refs.py` 和本次接入修改。
其中工具清理、提示词及中间件改动是既有工作，本次没有重写其设计。
历史实验、日志、原始输出和报告附件保留原位，不纳入产品源码提交；
未删除历史数据，也未将私有凭据加入 Git。

- `uv sync --frozen --all-packages --group dev` 通过。
- `uv run ruff check src tests scripts` 通过。
- 全仓 Ruff 首次检查 59 项；本次修正产品源码两处空白后，历史实验目录仍有 57 项问题。
- Pyright：0 errors / 0 warnings。
- 编码检查通过，UTF-8 无 BOM。
- 适配器专项：42 passed，包含 Standard / Ultra 创建请求的边界检查。
- 前端冻结依赖安装及构建通过；构建保留体积提示。
- Worker 干净依赖安装及构建通过。
- 完整 unit/integration：1134 passed / 4 skipped / 46 warnings，耗时 512.49 秒。
  全量启动后补充的模式传递和记录字段由最终 42 项适配器专项覆盖，未再次跑全量；数量不累加。
  跳过项为两个 Windows 符号链接权限用例、显式 Kali 附件验收和未提供密钥的真实模型兼容检查。

## 个人部署核验

通过原 `start-workbench.cmd --api-only --no-browser --smoke-seconds 5` 完成启动检查：
数据库、SSH、Executor 和 Ultra Worker/A2A 检查通过，控制台 API 正常启动，
随后启动器清理本次创建的服务。没有启动通用 Runner 去恢复历史任务。
本次是 API-only 启动核验，不代表完整模型运行已验收；服务未被改为后台常驻。

Kali 已安装 25 个 Python 文件与本机源码哈希一致，身份与能力检查通过，
活动 Job 为 0；执行端没有代码变更，保留原进程，启动身份前后一致。

前后只读快照显示 8 个非终态 Run（5 paused / 3 recovering），
状态摘要和本机配置文件摘要一致。没有续跑、取消或清理这些任务。
快照保存在仓库外的 `C:\Users\HP\.workbench\tsecbench\`，不含模型密钥值。

## 凭据到位后还需完成

1. 保存本轮配置并核对其确属 Tsecbench v1，不复用旧 104 题任务的令牌。
2. 实测 Windows 与 Kali 的 VPN 路由及同一配置的并发连接限制。
3. 只读拉取本轮题目并核对题量、flag 总数和有效期。
4. 启动用户选定的真实单题，验证 Ultra、模型、目标访问、结果提交及资源释放。
5. 单题通过后，再按平台整场时限安排串行题集调度；如需自动批量，另行补齐该入口。

本地评测与托管上榜不同；当前没有上传镜像、发布成绩或推送 Git。

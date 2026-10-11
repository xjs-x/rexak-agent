# 工具进度、字体及 HTTPS 错误修复验收

日期：2026-10-11。以实际代码和可复现证据为准。

## 本次修改

聊天按时间顺序显示工具活动，默认折叠。展开可查看实际工具参数、返回结果、操作与后台任务标识。处理中、等待结果、失败及打断等待分别显示；后台任务以执行台账的已同步状态为准。断线时不将旧记录冒充当前终态。显示的命令或参数来自真实调用，没有根据工具名虚构 shell 命令。

工具开始及返回时写入有界展示事件；本地工具与网络工具均覆盖。历史外部操作从现有操作表读取，只有展开旧记录时才读取官方 checkpoint 参数和 ToolMessage。上下文整理移出的旧参数如实说明不可还原，没有增加第二份可写记忆或运行状态真源。密码、授权头、Cookie、嵌套 JSON 等先脱敏再限制长度；展示故障不改变真实操作结果。

页面正文改为 `#202123`，次要文本统一为 `#5d626a`；提示、输入占位、工具记录和详情文字同步加深。滚动查看旧记录时不会因后续输出强制跳回底部。

## HTTPS 根因与修复

最近实际任务 `2c260653-8077-4180-937b-3e025ef7794a` 的两次 HTTPS 请求，执行端原始错误为 `CERTIFICATE_VERIFY_FAILED: self-signed certificate`。请求没有形成响应，控制端仍下载 `response.meta.json`，下载 404 覆盖了证书错误，同时使控制端操作记录滞留 `running`。

现在先读取远端终态：失败请求不下载响应工件，保留 operation_id/job_id 和原始原因，更新操作终态。执行端区分证书错误、连接错误、超时及其他 HTTP 传输错误；证书错误不建议原样重试。历史进度读取也能将已同步的远端失败状态显示出来。

HTTP 工具新增 `verify_tls`，默认为 true。对自签名测试服务可显式设置 false 发起新的逻辑请求，结果注明证书未验证；只影响这次请求及其重定向链。身份的默认连接池继续校验证书，同身份 Cookie 保留、不同身份不共享，逐跳范围检查继续生效。没有增加目标特定参数默认值或提示词。

## 验证证据

- 实际 Edge 浏览器：工具运行前即显示进度；默认折叠；展开可读取脱敏参数；完成后自动更新返回内容；错误工具显示失败；刷新后记录仍可展开。使用官方 `create_agent`、真实 PostgreSQL 和可复现模型，本地 fixture 工具不向目标发送流量。结果：[tool-activity-ui.json](tool-activity-ui.json)，截图：[折叠](tool-activity-collapsed.png)、[展开](tool-activity-expanded.png)。
- 实际旧任务只读浏览器验收：历史 HTTPS URL 参数及原失败结果可展开查看。结果：[tool-activity-history.json](tool-activity-history.json)。原历史错误不被改写为修复后的结果。
- Windows 自签名 HTTPS fixture：2 passed，覆盖默认拒绝、单次关闭、再次默认拒绝、Cookie 身份隔离及重定向范围检查。
- Kali 上同样的 HTTPS fixture：2 passed，第三方 Starlette 测试客户端弃用警告 1 条；见 [执行端测试日志](tls-kali-tests.log)。执行端 wheel 已上传，文件哈希、安装依赖与重启健康检查通过。
- 实际指定目标只做三次 HEAD：校验开启 → `tls_certificate_error`；单次关闭 → HTTP 200 且 `tls_verified=false`；再次开启 → `tls_certificate_error`。无工件 404，未调用模型；见 [https-head.json](https-head.json)。这证明 HTTPS 工具链修复，不代表渗透效果或证书可信。

控制端完整回归：304 passed、3 skipped（两个 Windows 符号链接权限测试、一个未提供凭据的真实模型测试）。最后补充默认 TLS 参数的历史请求摘要兼容检查，工具活动、操作持久化与派发守卫定向回归 39 passed。Ruff、Pyright、编码检查与前端生产构建通过，`git diff --check` 通过。

## 使用

Kali 执行端已更新。控制端 API 和 Runner 需要在原启动窗口按 Ctrl+C 后重新双击 Rexak Agent，刷新网页加载新界面。展开任一工具行即可查看进度详情，无需打开“任务详情”。

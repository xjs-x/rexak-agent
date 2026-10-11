# P3–P5 部署与当前能力边界

执行岛服务可使用 `deploy/rexak-executor.service` 作为 systemd 模板。先在执行岛
安装独立的 `workbench-executor` 包、配置 `.env` 中的 `EXECUTOR_TOKEN` 与
`EXECUTOR_DATA_ROOT`，并将 `.env` 权限设为仅服务用户可读。模板默认用户和路径为
`kali` 与 `/home/kali/rexak-agent-executor`，部署到其他位置需按实际路径修改。

```bash
sudo cp deploy/rexak-executor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rexak-executor
sudo systemctl status rexak-executor
```

`workbench doctor --skip-model` 会检查数据库和执行岛；如需从执行岛验证目标连接，
另传 `--target-url URL --scope-file scope.json`。范围文件与 Executor 的 `ScopePayload`
一致，需包含 `rules`，必要时再显式写 `mappings`。探针报告实际连接 URL、DNS 解析地址、
TLS 握手是否通过和 HTTP 状态。`127.0.0.1` 总是指执行岛本机；控制机本地站点需显式
映射。HTTPS 映射到不同主机名时，当前实现不能保证原始 SNI，探针会报告 `unsupported`。

浏览器状态按 session 和身份分离，成功动作后原子保存 Cookie、LocalStorage 与
IndexedDB。`sessionStorage` 不在 Playwright `storage_state` 中，不能保证重启后恢复。
恢复时可传登录探针 URL 和已登录元素选择器；状态文件存在不等于仍然有效。
浏览器登录所用密码由控制端 `secret_ref` 解析，执行岛 SQLite 台账仅保留脱敏参数。
同身份的浏览器 Cookie 可复制给执行岛 HTTP 会话，复制时保留域、路径及 Secure 属性。
CSRF 短时值应由当前浏览器页面或当前 HTTP 响应获取，在表单操作中实时使用，不能写进
长期经验。HTTP 工具提供 `csrf_source_url`、`csrf_input_name` 与注入位置参数；每次写请求
先获取新的隐藏表单值，再在同一身份锁内注入当前请求，不把 token 返回给模型。

通用命令的无人值守能力仍关闭。仅配置 `EXECUTOR_NETWORK_ISOLATION=enforced`
不能构成出站网络隔离；当前版本没有可以验证的内核级范围边界。进程控制、工作区文件、
取消与断线核对代码仍可做隔离环境内的专项测试，但模型不会被授权运行任意命令。

自动记忆提炼只从完成的 run 中抽取带工件引用的观察，写为项目 `candidate`。
网页内容保持不可信属性；只有带验证引用的显式提升才成为 `verified`。上下文压缩
失败会让 run 进入恢复状态，旧 checkpoint 消息不会被提交删除。

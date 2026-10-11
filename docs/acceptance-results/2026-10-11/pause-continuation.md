# 暂停、续聊与普通 URL 指令验收

日期：2026-10-11。以实际代码和可复现证据为准。

## 已复现的问题

实际任务记录包含 `running -> paused` 非法迁移及“旧阶段的图仍有待执行节点”错误；任务范围为 `mode=scoped, rules=[]`。暂停指令在抛出打断异常时回滚了消费记录，后续输入又可能撞上旧图的工具节点。旧客户端发送空 scope 时也没有启用自动目标模式。

## 数据库与官方图验证

`tests/integration/test_pause_continuation.py` 的五项测试通过，使用真实 PostgreSQL。覆盖暂停消费事务提交、合法暂停迁移、普通消息恢复同一 Run、失败后新 Run、模型等待取消及旧客户端空范围兼容。

两种 checkpoint 续聊测试使用官方 `create_agent`、`AsyncPostgresSaver` 和可复现模型。在旧图下一节点为 `tools` 时暂停或失败，再发送普通 URL 指令：旧待执行节点清空，历史保留，新消息进入模型，旧工具的执行记录仍为空。补充的 ToolMessage 只标记中断，不产生虚构工具结果。

完整控制端回归：`pytest tests/unit tests/integration tests/recovery -q`，297 passed、3 skipped。跳过项为两个 Windows 符号链接权限测试和未提供模型凭据的真实模型测试。Ruff 与 Pyright 通过，编码检查 235 个文件通过，前端生产构建及 `git diff --check` 通过。本次涉及的十个 Python 文件通过格式检查；全仓库格式检查仍报告 29 个既有文件需要排版，未为此改动无关文件。

真实模型测试未运行，本次验证不代表渗透效果通过验收。

## 实际浏览器验证

运行 `scripts/accept_pause_ui.py --base-url http://127.0.0.1:8780`，使用独立 API 和 Edge 浏览器；通过实际点击及真实定向 Runner 验证，不调用模型。

1. 创建带普通 URL 的聊天任务，点击暂停，落库为 `paused`；输入框与发送按钮继续可用。
2. 旧聊天保持暂停时，新建第二个聊天成功。
3. 回到暂停的聊天，发送“对这个网站继续进行测试”及新 URL；同一 Run 排队，目标自动追加，无需手动配置 scope。
4. 两个验收聊天通过界面删除，定向 Runner 完成取消；没有修改日常用户任务。
5. 浏览器没有未处理的页面错误。

结果文件：[pause-ui.json](pause-ui.json)。截图：[暂停后可直接输入](pause-ready-for-input.png)。

## 启用

控制端 API 与 Runner 需要重新启动，刷新网页即可使用新版本。无需更新 Kali Executor，无需数据库迁移。旧的失败任务可以直接发送新的普通 URL 测试指令；旧待执行工具不会在新阶段自动重放。

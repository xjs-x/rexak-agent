# 对话思考正文展示修复

日期：2026-11-07。未提交、未推送。

- 最近8次模型正文归档中存在前置 `<think>…</think>`，供应商将思考标记直接放入普通正文；原展示层将其按普通文本铺开。没有回滚或更换页面样式。
- 新增 `api/assistant_display.py`，`api/routes.py` 在生成对话预览及正文分页前移除前置思考段；旧记录无需重写。用户指令、普通正文中的引用和代码示例保留。
- 新增 `web/src/console/assistantDisplay.ts`；`ChatView.tsx` 对实时累计输出和历史消息做展示投影。流式标签尚未完整到达、思考段尚未闭合时也不展示其内容；只保留简短思考状态。没有正文的纯思考消息不生成空白正文气泡；正常答复沿用 Markdown 和原工具活动布局。
- 没有修改模型请求、Checkpoint、缓存布局、原始回复工件或历史证据，也未发起真实模型/靶场实验。
- 专项：后端12 passed、前端15 passed；定向 Ruff/Pyright、编码（778文件）、前端构建和变更空白检查通过。未重复全量测试。
- 使用原 `start-workbench.cmd --no-recover --no-browser` 加载新版，保留历史任务恢复状态。在线核对最新8条实际对话：预览、分页正文与隐藏思考后的展示一致，原工件哈希一致；控制台与最终构建一致。
- 部署前后9个历史非终态Run摘要、配置摘要及运行数据计数保持一致；Kali26个已安装文件哈希、身份和能力核对通过、active_jobs=0；执行端未变，保留现有进程。
- 浏览器可视化检查因自动化工具无法可靠识别当前URL而停止，未继续浏览器操作；以DOM专项和在线接口核验作为本次证据，不宣称完成实际页面截图验收。
- 私有证据：用户目录 `.workbench/assistant-display-before.json`、`assistant-display-after.json`、`assistant-display-smoke.json` 和 `assistant-display-launch.log`。

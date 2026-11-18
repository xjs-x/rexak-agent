# Web 模型设置与官方请求预算

入口为桌面 Rexak Agent → `start-workbench.cmd` → `scripts/start_workbench.py`。

## 旧配置迁移时保留的参数

以下是 2026-10-15 核对的内置档案。2026-11-18 的设置改版保留这些档案，用户也可以配置其他提供商、型号及请求预算。

| 供应商 / 模型 | 上下文窗口 | 单次完整输出上限 | 本机允许输入估算上限 |
| --- | ---: | ---: | ---: |
| 小米 `mimo-v2.6-pro` | 1,048,576 | 131,072 | 825,753 |
| 小米 `mimo-v2.6-flash` | 1,048,576 | 131,072 | 825,753 |
| 小米 `mimo-v2.6-pro-ultraspeed` | 1,048,576 | 131,072 | 825,753 |
| 百炼 `deepseek-v4.1-flash` | 1,000,000 | 393,216 | 546,105 |

来源核对日期为 2026-10-15：

- [小米模型目录](https://mimo.mi.com/docs/quick-start/model)列出三款模型的 1M 上下文与 128K 输出；[官方接入配置](https://mimo.mi.com/docs/en-US/tokenplan/integration/openclaw)明确给出上下文整数 1,048,576；[Chat API](https://mimo.mi.com/docs/en-US/api/chat)明确 `max_completion_tokens` 的默认值及上限为 131,072。
- [百炼 Flash 模型页](https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash)明确给出 1,000,000 上下文与 393,216 输出。[百炼兼容 Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)支持并推荐新参数 `max_completion_tokens`，覆盖思考与回答。[DeepSeek 原厂目录](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)作为用户截图的交叉参考，不替换百炼的模型 ID 或业务空间接入地址。

两家的“1M”不能直接换成同一个整数。小米 ultraspeed 使用模型目录给出的同档 1M，按该系列官方整数配置；该账户是否开通 ultraspeed 服务仍以供应商为准。

初次请求与恢复请求都显式使用官方最大完整输出额度，不再沿用原来的 8,192。采样温度默认 1.0，不自行限制思考长度；小米思考模式的温度与 top_p 遵循供应商规则。实际输出可以远小于上限，上限不意味着每次必定消费这些 tokens。

输入预算保持现有公式：`floor((context_limit - actual_request_output_limit) × 0.9)`，若另有独立最大输入额度则取更小值。计数仍基于实际序列化载荷，包括系统提示、工具 schema、历史、工具结果及必须回传的 `reasoning_content`。这是本机估算，不能冒充官方 tokenizer 或账单。恢复前也重新按实际输出额度计数。

旧字段 `WORKBENCH_OUTPUT_RESERVE_TOKENS`、`WORKBENCH_RECOVERY_OUTPUT_LIMIT_TOKENS`、`DASHSCOPE_CONTEXT_LIMIT` 仅兼容读取，不再覆盖这四款模型的官方档案。上下文压缩触发比例、单次请求空闲/墙钟超时和恢复次数保持原有资源边界；没有新增任务总时长、总费用限制。能力探测状态保持 unknown，不用文档声明冒充本机真实模型验证。

## 使用与保存

1. 双击原桌面入口。首次未配置 Key 时可正常打开控制台，Runner 等待必要配置。
2. 在左侧「模型设置」新增或编辑提供商，填写 Provider ID、显示名称、API Base URL 和 API Key。同一提供商的模型共享连接设置和 Key。小米与百炼的已有连接可直接编辑。
3. 点击「获取可用模型」可用当前表单查询目录，搜索并勾选型号后添加；也可直接填写真实 Model ID。完成型号、能力与默认模型配置后点击「保存提供商」。编辑已有连接时，同源地址可留空保留 Key；勾选「保存时清除 Key」会撤销当前凭据槽的 Key。
4. 输入框上方选择新聊天或下一运行阶段的模型；创建任务与发送消息都会携带该选择。当前活动或暂停阶段固定原模型与接入地址，界面锁定选择；普通补充与继续沿用当前阶段。需要另一个模型时可新建聊天，结束后的下一阶段也可以切换。

非秘密偏好保存在 `WORKBENCH_DATA_ROOT/settings/models.json`，原子替换；API 与 Runner 读相同配置，不依赖启动器内存传 Key。
模型密钥单独位于 `WORKBENCH_SECRET_STORE_PATH` 同目录的 `model-secrets.json`，默认在当前用户 `.workbench` 目录。Windows 用当前用户 DPAPI 加密，POSIX 用 0600 本机文件；该文件不进业务数据库、运行工件和标准数据库/工件备份，也不下发 Kali。更换 Windows 用户或电脑时重新填写 Key。
API 只返回是否配置；校验错误不回显输入值，浏览器不在 localStorage 保存 Key。配置保存后新阶段或暂停后重新装配读取新 Key；已在执行中的模型请求不会被中途替换。端点变更用于新阶段，当前阶段仍保留其快照端点。

## 旧阶段兼容与实现位置

档案版本从 `1` 升级为 `official-2026-10-15`。旧阶段在 Runner 下一次装配时升级内置模型的错误上下文/输出预算，并记录旧参数与变更事件；固定模型、Base URL、密钥引用、checkpoint 和 operation_id 不变。旧默认温度 0 随官方档案升级为 1。历史调用台账不回写，恢复次数台账不清空。旧 `env:` 引用兼容读取对应供应商已保存的 Key。

- `config/catalog.py`：四款模型与来源；`models/provider.py`：档案解析与旧预算升级。
- `config/model_settings.py`：偏好、DPAPI 与供应商 Key；`config/secrets.py`：统一引用解析。
- `api/model_settings.py` / `contracts/models.py`：受会话、Origin 和 CSRF 保护的模型设置、目录发现及协议定义。`api/routes.py` 负责阶段模型选择与快照。
- `runtime/runner.py`：无 Key 时等待、恢复预算升级及审计；框架主循环仍为官方 `create_agent`。
- `web/src/ModelConfiguration.tsx` / `App.tsx`：全局设置、任务设置及输入框模型选择。

2026-11-18 改版的验证范围见[测试与验收](../reports/model-settings-redesign/tests.md)。2026-10-15 的历史参数验收见[原验收记录](acceptance-results/2026-10-15/model-settings/README.md)。

# 模型设置设计

提供商列表显示名称、Provider ID、协议、地址摘要、Key 状态、已添加型号数量和启用状态。点「查看模型」展开目录，点「编辑」进入单独的表单。模型设置入口和任务设置复用同一套组件。

连接表单用当前草稿查询模型目录。结果在可搜索、可滚动的对话框中打开，初始选择为空。「全选筛选结果」保留之前选中的型号，「取消全选」清空选择。已存在的型号可以查看，重复项无法选中。批量添加先更新草稿，点击「保存提供商」才写入本机配置。

模型编辑器保存真实 Model ID、显示名称、启用状态、默认模型选择和声明的能力。最大上下文、输入、输出与单次请求输出预算分别保存。新发现型号的能力维持未知，用户可自行声明 Thinking、Function Calling 和图像输入支持。只有目录来源可辨认的数值规格会进入建议值。

数据继续存放在 `WORKBENCH_DATA_ROOT/settings/models.json`，密钥留在独立的本机密钥库。Provider ID 可自定义，保存后保持稳定。模型由 Provider ID 与 Model ID 共同标识，两个提供商可使用相同的 Model ID。显示名称可用中文和空格，可随时更改。新凭据槽使用独立 UUID，便于区分同一 Provider 在不同 API 主机上的历史绑定。

实际参考了 DeepSeek Harness 的提供商填写、草稿查询、型号候选和主机端协议分发。Rexak 的提供商表单沿用浅色视觉，目录选择使用原生模态对话框。[DeepSeek Harness 配置文档](https://deepseek-harness.github.io/deepseek-harness/guide/providers.html) · [ProviderEditor](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-settings-models/src/client/ProviderEditor.tsx) · [ModelListEditor](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-settings-models/src/client/ModelListEditor.tsx) · [Discovery](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/src/discovery.ts)

# 模型目录发现

前端发送 `POST /api/settings/models/discover`，请求体包含当前推理地址、目录格式、可选独立目录 URL、所编辑的已保存 Provider ID 及临时 Key。接口要求本地会话、来源检查和 CSRF。临时 Key 可立即替换本次查询使用的凭据，保存前不会写入密钥库。

| 目录格式 | 上游请求 | 响应结构 |
| --- | --- | --- |
| OpenAI | `GET {base}/models`，也可指定独立目录 URL | `data[].id` |
| 阿里云百炼 | `GET {models_url}?page_no=1&page_size=50`，按页读取 | `success`、`output.models[].model`、`output.page_no/page_size/total` |

两种格式均以 Bearer Header 发送 Key。OpenAI Base URL 会去掉末尾斜杠，并把完整的 `/chat/completions` 输入还原成相应 Base URL。系统保留路径前缀和已有 `/v1`。独立目录 URL 按填写的路径请求。百炼格式由用户主动选择并填写目录地址。[阿里云百炼模型列表文档](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/list-models)

HTTPS 支持远程和自定义网关，本机 HTTP 仅接受回环主机。地址禁止内嵌认证、查询参数和片段。保存的 Key 仅可在推理与目录地址都仍属于保存时的来源时复用。跨来源目录需输入临时 Key。连接不继承环境代理，禁止自动重定向。服务端向浏览器返回错误类别和上游状态码，供应商原始错误体不会回传。

单次查询最多 25 秒，每页最多 4 MiB，整次至多读取 40 页并保留 2,000 个唯一 Model ID。客户端取消或离开页面会中止请求，服务端监听断开并取消上游连接。错误时可以继续手动添加。成功返回的空目录会在选择对话框内明确显示。

2026-11-18 对原百炼北京工作空间发出一次只读目录请求，实际路径为 `GET /compatible-mode/v1/models`。响应为 HTTP 200，共 184 个唯一 Model ID，包含 `deepseek-v4.1-flash-1017`。当前 Base URL 已支持标准发现。独立 `/api/v1/models` 使用本地 Mock 验证了格式与分页。

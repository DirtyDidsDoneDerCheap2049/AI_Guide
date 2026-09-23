# 对话模型与深度思考

首页和对话输入框都提供模型选择与思考档位按钮。按钮直接显示“关闭”“轻度”“标准”或“深入”，点击后展开分档滑条，也可以直接点击档位名称或使用方向键。选择保存在当前浏览器；每次发送把模型、档位和预算存入任务快照。改档位只影响下一条消息，已经排队的任务和服务端重试沿用原选择。回答旁显示本次使用的模型和是否启用深度思考。

只接入 **OpenAI Chat Completions 格式**：向 `BASE_URL/chat/completions` 发送 `messages`，读取 `choices[0].message.content` 和 `usage`。不支持 Anthropic、Gemini 的原生协议或 Responses API。兼容格式不代表所有扩展参数都相同，需要为每个模型选择适配方式。

## 沿用当前配置

已有 `TEXT_API_BASE_URL`、`TEXT_API_KEY`、`TEXT_MODEL` 可以继续使用。新增变量默认为：

```dotenv
TEXT_MODEL_PROFILES=[]
TEXT_THINKING_MODE=auto
VISION_THINKING_MODE=auto
```

列表为空时，页面只展示 `TEXT_MODEL`。`auto` 仅对已识别的 DashScope 域名启用 Qwen 参数，其他接口不发送思考扩展，也不提供可用的思考开关。使用代理域名时，请明确设置 `TEXT_THINKING_MODE=qwen`，不要根据模型名猜测协议。

## 配置多个模型

在服务器 `.env` 添加一行 `TEXT_MODEL_PROFILES`。它是 JSON 数组，第一项是默认模型。下面示例复用已有文本接口和 Key，单价只是示例，部署前按供应商计价填写；单位与现有预算相同，为每千 token 的费用。

```dotenv
TEXT_MODEL_PROFILES='[{"id":"qwen-plus","label":"Qwen Plus","model":"qwen3.7-plus","thinking_mode":"qwen","prompt_price":0.002,"completion_price":0.008},{"id":"qwen-flash","label":"Qwen Flash","model":"qwen3.8-flash","thinking_mode":"qwen","prompt_price":0.0008,"completion_price":0.0027}]'
```

模型名必须是当前账号确实可调用的 ID。不是把任意名称加进数组就能使用。选项由服务器管理员配置，访客不能输入任意接口地址或提交 Key。

| 字段 | 用途 |
|---|---|
| `id` | 页面提交的稳定标识，只能含字母、数字、下划线和短横线 |
| `label` | 页面显示名称 |
| `model` | 发给供应商的真实模型 ID |
| `base_url` | 可选；默认 `TEXT_API_BASE_URL`，填写接口前缀，不带 `/chat/completions` |
| `api_key_env` | 可选；默认 `TEXT_API_KEY`。跨供应商时填写另一环境变量名，真实 Key 写在该变量中 |
| `thinking_mode` | `none`、`qwen`、`deepseek` 或 `reasoning_effort` |
| `json_mode` | 默认 `true`；接口不支持 `response_format` 时设 `false`，后端仍校验返回 JSON |
| `prompt_price` / `completion_price` | 必填；该模型输入和输出价格，用于任务预留和记账 |

跨供应商可设置 `"base_url":"https://api.example.invalid/v1","api_key_env":"OTHER_MODEL_API_KEY"`，再在 `.env` 配置 `OTHER_MODEL_API_KEY`。示例域名不可直接使用。Key 只在服务端读取，不写入任务快照或模型列表接口。

## 开关实际发送什么

| 模式 | 关闭 | 开启 |
|---|---|---|
| `none` | 不发送思考参数 | 页面禁用，API 拒绝 |
| `qwen` | `enable_thinking: false` | `enable_thinking: true`，并发送 `THINKING_BUDGET` |
| `deepseek` | `thinking: {type: "disabled"}` | `thinking: {type: "enabled"}` |
| `reasoning_effort` | `reasoning_effort: "none"` | `reasoning_effort: "low" / "medium" / "high"` |

Qwen 的“轻度 / 标准 / 深入”分别使用 `THINKING_BUDGET` 的四分之一、原值、两倍；默认是 1024 / 4096 / 8192 token。这是最大思考预算，不保证模型实际用满，也不保证回答更准确。

`reasoning_effort` 的三个档位对应 `low / medium / high`，关闭对应 `none`，仅适用于支持这些值的模型。DeepSeek 适配目前只提供“关闭 / 开启”，不会展示并未实现的强度区别。`none` 表示本站不控制思考参数，不保证供应商内部没有推理过程。常开推理或只能流式调用的模型暂不纳入可切换列表。

深度思考在 Worker 内执行，页面通过 SSE 接收任务状态，不显示内部推理文本。开启后不发送温度；Qwen 思考模式不强制 `response_format`，避免部分模型拒绝这一组合。结构校验和有限修复仍执行。空内容、超时或错误响应显示失败，不会假装完成或自动换模型。

开关控制**对话回答**。照片识别继续使用 `VISION_MODEL` 与 `VISION_ENABLE_THINKING`，因为所选文本模型不一定支持图片。

## 更新与验证

修改 `.env` 后重新创建 API 和 Worker，`restart` 不会重新加载容器环境变量：

```bash
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml up -d --no-deps --force-recreate api worker
```

浏览器刷新后检查列表。分别选择两个模型发送问题，再对同一问题开关深度思考；检查回答旁标识及 `ToolInvocation` / `InvocationAttempt` 用量。浏览器请求不应出现 Key。协议桩验证请求格式和失败路径；真实账号权限、效果、延迟与收费另行验证。

深度思考可能更慢且消耗更多 token。超时由 `PROVIDER_TIMEOUT_SECONDS` 控制。若增大超时，同时保证 `RUN_LEASE_SECONDS` 大于一次请求的最长等待并留出写回余量；不要只把超时无限拉长。费用预算是估算控制，不是供应商账单的绝对硬上限。

参数依据：[Qwen 思考模式](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)、[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。服务商参数支持可能变化，以实际接入验证为准。

# AI 接口接入与上下文管理

工作台按能力选择后端，不再限定 Codex。现有 `LARKX_AGENT_BACKEND=codex` 可继续使用；不会自动切换供应商，也不会自动把现有聊天发送给新服务。

## 接入兼容 HTTP 服务

在项目 `.env` 中配置以下字段，重启后生效：

```dotenv
LARKX_AGENT_BACKEND=compatible
LARKX_AI_BASE_URL=https://你的服务地址/v1
LARKX_AI_API_KEY=你的密钥
LARKX_AI_MODEL=供应商提供的模型名称
LARKX_AI_TIMEOUT=180
LARKX_AI_OUTPUT_TOKEN_FIELD=max_completion_tokens
LARKX_AI_IMAGES=false
LARKX_AI_CACHE_KEY=false
```

适配器调用 `POST <BASE_URL>/chat/completions`。本地模型服务可以使用 `http://127.0.0.1:端口/v1`，无需鉴权时密钥可留空。其他地址必须使用 HTTPS。仅支持旧参数的服务可将输出参数改为 `max_tokens`。

工作台要求服务和模型支持 `response_format.type=json_schema`、`strict=true`。返回结果还会在本地按 Schema 验证；拒绝回答、工具调用、截断输出、无效 JSON 和网络错误均不交给业务层执行。不会通过自动去掉 Schema、切换模型或重试收费请求来掩盖不兼容。图片需明确开启 `LARKX_AI_IMAGES=true`，并由所选模型支持；支持 JPEG、PNG、WebP、GIF，每张至多 8 MiB。

## 模块边界与扩展

| 模块 | 职责 |
| --- | --- |
| `larkx/agent/contracts.py` | 请求、结果、能力和 token 用量结构 |
| `larkx/agent/context.py` | 历史文件、有限内存缓存、过期、输入估算与历史选择 |
| `larkx/agent/model_backend.py` | 会话串行、请求组装、预算、重置并发保护、统计 |
| `larkx/agent/codex_backend.py` | Codex CLI 调用、沙箱、超时与错误处理 |
| `larkx/agent/compatible_backend.py` | HTTP 连接池、接口映射、响应校验和用量解析 |
| `larkx/workhub/ai_context.py` | 去掉已在消息正文出现的重复历史记录 |
| `larkx/workhub/runtime.py` | 当前账号作用域、工作台生命周期和状态出口 |

非兼容服务只需实现 `ModelBackend.generate(request)`，在启动工作台前注册工厂。不要在适配器中发送飞书消息、修改待办或复用一个全局供应商会话。

```python
from larkx.agent import (
    ModelBackend, Capabilities, GenerationResult, TokenUsage, register_backend,
)
from larkx.config import DATA_DIR

class MyBackend(ModelBackend):
    name = "my-provider"
    capabilities = Capabilities(structured_output=True, images=False)

    def __init__(self):
        super().__init__(DATA_DIR / "my-provider-history")
        self.model = "your-model"
        # 在这里初始化供应商 SDK 客户端。

    async def generate(self, request):
        # 将 request.system、messages、response_schema 映射到供应商 SDK。
        # 如声明支持结构化输出，必须保证/验证输出满足 response_schema。
        # 将 request.max_output_tokens 用作输出上限。
        # 如果供应商支持缓存提示，使用 request.cache_key；它不是授权凭据。
        result = await your_sdk_generate(request)
        return GenerationResult(result.text, TokenUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cached_tokens=result.cached_tokens,
        ))

    async def aclose(self):
        # 关闭此适配器持有的 SDK/HTTP 客户端。
        pass

register_backend("my-provider", MyBackend)
# 随后用 LARKX_AGENT_BACKEND=my-provider 创建 HubRuntime。
```

`your_sdk_generate` 是接入方实现的映射函数。`GenerationResult.usage` 的未知值保持 `None`，不要将未知命中量写成零。注册名重复或未知的后端名会明确报错。

绕过工作台直接调用适配层时，调用方必须提供隔离后的会话键，例如 `scoped_key(account_id, "chat", chat_id, thread_id)`（从 `larkx.agent.contracts` 导入），再调用 `await backend.run(key, prompt)`；不得对不同用户复用固定字符串。`system_prompt` 参数可单独传入稳定规则，避免把每轮变化的数据放在规则之前。

## 会话隔离

消息入口使用账号、聊天和话题生成无歧义的哈希键。工作台另加用途维度，区分分析、控制台和草稿；观察消息也带上话题。HTTP 历史目录同时区分服务地址和模型。历史文本、历史图片、并发锁、清除操作和供应商缓存键均使用同一会话边界。

群聊的同一话题保留成员共同参与的上下文，不按发言人拆散正常讨论。默认 `anchor` 隔离话题；普通 CLI 显式配置 `chat` 可共享同一聊天的不同话题，旧 `global` 配置降为 `anchor`，不再跨聊天共享。工作台不采用 `global` 或 `chat` 放宽模型会话边界。同一聊天的角色、概览和长期记忆仍是现有业务层共享背景。

旧的无账号命名空间模型历史不自动迁移，新键从新会话开始。清除会话后，正在生成的旧结果会被拒绝写回和返回；不同会话可并行，同一会话串行处理。

当前应用仍是本机单账号工作台，不是带登录鉴权的多租户服务器。多账号同时部署必须分别设置 `LARKX_HOME`、消息数据库和监听端口；不能只靠模型缓存键隔离数据库或页面访问。不要把本地接口直接当作多用户公网服务。

## Token 预算与缓存

```dotenv
LARKX_AI_INPUT_TOKEN_BUDGET=16000
LARKX_AI_HISTORY_TOKEN_BUDGET=6000
LARKX_AI_MAX_OUTPUT_TOKENS=2048
LARKX_AI_CONTEXT_TTL=86400
```

输入预算包括规则、当前请求和 Schema 的估算消耗，并给图片预留空间；只从剩余预算取最近历史，不截断当前指令。当前请求自身超预算会明确报错。估算使用 UTF-8 字节数，不是特定模型的精确 tokenizer，也不是计费数字；图片实际消耗依模型和分辨率变化。HTTP 输出受配置上限控制；Codex CLI 输出上限由 CLI/模型控制，本配置不虚构 CLI 支持的参数。

普通聊天默认 `history_mode="conversation"`，复用有限历史；结构化工作请求默认 `history_mode="snapshot"`，因为业务层已提供当前概览、记忆和任务，不再回放旧快照或旧图片。SDK 接入可明确指定模式。历史默认最多保留 20 条消息，过期后不再用于生成；过期不等于磁盘文件已删除。

本地内存缓存最多 128 个会话，减少文件读取，本身不减少模型计费 token。实际节省来自删除正文与上下文里的重复消息、限制历史和不重复回放快照。分析规则移到动态资料前，当前时间移到请求末尾，为前缀复用创造条件；规则、模型、Schema 或前缀变化仍可能导致缓存失效。

只有供应商明确支持 `prompt_cache_key` 时才打开 `LARKX_AI_CACHE_KEY=true`。这个字段是缓存路由提示，不是缓存命中保证、权限控制或存储引用。适配器不会自行删掉已缓存的输入文本，也不会缓存并重放旧回答。平台仍需接收完整请求来匹配前缀；命中条件、最低长度、保留期限和费用以[官方缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)及对应供应商文档为准。协议字段参考 [Chat API](https://developers.openai.com/api/reference/resources/chat)。

`GET /api/state` 的 `status.ai_usage` 提供当前进程成功调用累计计数：`calls`、`estimated_input_tokens`、供应商报告的 `input_tokens` / `output_tokens` / `cached_tokens`。`usage_reports`、`cache_reports` 表示实际收到这些数据的调用次数；为零表示未报告，不能据此判断“零消耗”或“没有命中”。Codex CLI 当前不报告这些计费字段；只有 HTTP 返回的实际 `cached_tokens` 可用于确认命中。日志不记录完整聊天或 API Key。

适配层变更需要重启进程生效。新后端上线前，应使用脱敏数据检查真实供应商的 Schema、图片和用量字段，不能把本地协议模拟测试当作第三方服务已联调成功。

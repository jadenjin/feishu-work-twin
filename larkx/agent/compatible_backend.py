"""Chat Completions adapter; optional extensions must be explicitly enabled."""

import asyncio
import base64
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import DATA_DIR
from .contracts import Capabilities, GenerationResult, TokenUsage, scoped_key
from .model_backend import ModelBackend


class CompatibleBackend(ModelBackend):
    name = "compatible"

    def __init__(self, *, transport=None):
        self.base_url = os.environ.get("LARKX_AI_BASE_URL", "").rstrip("/")
        self.model = os.environ.get("LARKX_AI_MODEL", "").strip()
        address = urlsplit(self.base_url)
        if (
            not address.hostname
            or address.username
            or address.password
            or address.query
            or address.fragment
            or not self.model
        ):
            raise ValueError(
                "请配置 LARKX_AI_BASE_URL（如 https://服务地址/v1）和 LARKX_AI_MODEL"
            )
        if address.scheme != "https" and not (
            address.scheme == "http"
            and address.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("AI 接口必须使用 HTTPS，本机回环地址可使用 HTTP")
        self.capabilities = Capabilities(
            structured_output=True,
            images=os.environ.get("LARKX_AI_IMAGES", "false").lower() == "true",
            cache_key=os.environ.get("LARKX_AI_CACHE_KEY", "false").lower() == "true",
        )
        self.token_field = os.environ.get(
            "LARKX_AI_OUTPUT_TOKEN_FIELD", "max_completion_tokens"
        )
        if self.token_field not in ("max_completion_tokens", "max_tokens"):
            raise ValueError("LARKX_AI_OUTPUT_TOKEN_FIELD 配置无效")
        self.timeout = float(os.environ.get("LARKX_AI_TIMEOUT", "180"))
        if self.timeout <= 0:
            raise ValueError("LARKX_AI_TIMEOUT 必须大于 0")
        namespace = scoped_key(self.base_url, self.model)
        super().__init__(DATA_DIR / "ai_history" / namespace)
        headers = {}
        api_key = os.environ.get("LARKX_AI_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=self.timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self):
        await self.client.aclose()

    async def generate(self, request):
        messages = [{"role": "system", "content": request.system}, *request.messages]
        if request.images:
            content = [{"type": "text", "text": messages[-1]["content"]}]
            for filename in request.images:
                path = Path(filename)
                mime = mimetypes.guess_type(path.name)[0]
                if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    raise ValueError("AI 图片格式不受支持")
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("AI 图片超过 8 MiB，请先压缩图片")
                data = base64.b64encode(path.read_bytes()).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    }
                )
            messages[-1] = {"role": "user", "content": content}
        payload = {
            "model": self.model,
            "messages": messages,
            self.token_field: request.max_output_tokens,
            "stream": False,
        }
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "workhub_result",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        if self.capabilities.cache_key:
            payload["prompt_cache_key"] = request.cache_key
        try:
            # One overall deadline, including pooled connection wait and body read.
            # No automatic retry: an interrupted billable request may have succeeded.
            async with asyncio.timeout(self.timeout):
                response = await self.client.post(
                    self.base_url + "/chat/completions", json=payload
                )
        except (httpx.TimeoutException, TimeoutError):
            raise RuntimeError("AI 接口调用超时，未发送消息") from None
        except httpx.HTTPError:
            raise RuntimeError("AI 接口连接失败，请检查网络和服务地址") from None
        if response.status_code != 200:
            # Error bodies may contain echoed prompts or credentials.
            hint = {
                401: "请检查 API Key",
                403: "请检查接口权限",
                429: "额度或请求频率受限",
                400: "请检查模型是否支持 JSON Schema 和配置的输出 token 参数",
            }.get(response.status_code, "请检查 AI 服务状态")
            raise RuntimeError(f"AI 接口返回 HTTP {response.status_code}：{hint}")
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            if (
                choice.get("finish_reason") != "stop"
                or message.get("refusal")
                or message.get("tool_calls")
            ):
                raise ValueError("incomplete output")
            text = message["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty output")
            usage = data.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}

            def count(value):
                return value if type(value) is int and value >= 0 else None

            return GenerationResult(
                text,
                TokenUsage(
                    count(usage.get("prompt_tokens")),
                    count(usage.get("completion_tokens")),
                    count(details.get("cached_tokens")),
                ),
            )
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ):
            raise RuntimeError(
                "AI 返回空内容、截断内容或无效响应，未执行任何操作"
            ) from None

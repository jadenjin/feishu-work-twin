"""ChatGPT-account backend using Codex CLI to generate reply text."""

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger

from ..config import DATA_DIR
from .model_backend import ModelBackend
from .contracts import Capabilities, GenerationResult


def failure_reason(stderr):
    """Return a fixed safe message, never raw CLI output which can echo chats."""
    lines = (stderr or b"").decode("utf-8", errors="replace").splitlines()
    errors = [
        line for line in lines if line.strip().lower().startswith(("error:", "error "))
    ]
    diagnostic = "\n".join(errors or lines[-20:]).lower()
    if any(
        word in diagnostic
        for word in (
            "context_length_exceeded",
            "maximum context",
            "context window",
            "input too long",
        )
    ):
        return "context", "对话上下文过长，需缩减历史后重试"
    if any(
        word in diagnostic
        for word in ("invalid schema", "invalid_json_schema", "output schema")
    ):
        return "schema", "结构化回复格式不被模型接受"
    if any(
        word in diagnostic
        for word in (
            "model_not_found",
            "model is not supported",
            "model does not exist",
            "not supported when using chatgpt",
        )
    ):
        return "model", "当前账号不支持配置的模型，请检查模型名称"
    if any(
        word in diagnostic
        for word in ("not logged in", "unauthorized", "token expired", "401")
    ):
        return "auth", "登录凭据失效或未被识别，请执行 codex login"
    if any(
        word in diagnostic
        for word in (
            "usage_limit",
            "usage limit",
            "rate limit",
            "insufficient_quota",
            "429",
        )
    ):
        return "limit", "账号额度或请求频率受限，请稍后再试"
    if any(
        word in diagnostic
        for word in (
            "reconnecting",
            "error sending request",
            "failed to send request",
            "stream disconnected",
            "connection reset",
            "connection refused",
            "timed out",
            "tls",
            "network",
        )
    ):
        return "network", "连接 ChatGPT 失败，请检查网络或代理，登录状态可能正常"
    return "unknown", "调用未成功，原因未能安全识别；不一定是登录或模型问题"


class CodexBackend(ModelBackend):
    name = "codex"
    capabilities = Capabilities(structured_output=True, images=True)

    def __init__(self):
        self.executable = os.environ.get("LARKX_CODEX_PATH") or shutil.which("codex")
        if not self.executable:
            raise RuntimeError("未找到 Codex CLI，请先安装并执行 codex login")
        super().__init__(DATA_DIR / "codex_history")
        self.timeout = int(os.environ.get("LARKX_CODEX_TIMEOUT", "180"))
        self.model = os.environ.get("LARKX_CODEX_MODEL", "").strip()
        self.effort = os.environ.get("LARKX_CODEX_REASONING_EFFORT", "low")
        if self.effort not in ("minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("LARKX_CODEX_REASONING_EFFORT 配置无效")

    async def generate(self, model_request):
        request = model_request.as_text()
        response_schema = model_request.response_schema
        images = model_request.images
        with tempfile.TemporaryDirectory(prefix="larkx-codex-") as temp:
            output = Path(temp) / "reply.txt"
            args = [
                self.executable,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--disable",
                "shell_tool",
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "-c",
                f'model_reasoning_effort="{self.effort}"',
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-last-message",
                str(output),
            ]
            if self.model:
                args += ["--model", self.model]
            if response_schema:
                schema_file = Path(temp) / "schema.json"
                schema_file.write_text(json.dumps(response_schema), encoding="utf-8")
                args += ["--output-schema", str(schema_file)]
            for image_path in images:
                args += ["--image", image_path]
            args.append("-")
            # Separate cwd avoids loading project instructions into chat replies.
            deadline = time.monotonic() + self.timeout
            for attempt in range(2):
                # A failed CLI must not leave a stale reply for a retry to consume.
                output.unlink(missing_ok=True)
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=temp,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(request.encode("utf-8")),
                        timeout=max(0.01, deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    raise RuntimeError("Codex 生成回复超时，未发送消息")
                except asyncio.CancelledError:
                    proc.kill()
                    await proc.communicate()
                    raise
                if not proc.returncode:
                    break
                category, reason = failure_reason(stderr)
                if (
                    category == "network"
                    and attempt == 0
                    and deadline - time.monotonic() > 3
                ):
                    logger.warning("Codex 网络调用失败，重试一次（尚未发送飞书消息）")
                    await asyncio.sleep(1)
                    continue
                raise RuntimeError(f"Codex 返回错误 {proc.returncode}：{reason}")
            if not output.exists():
                raise RuntimeError("Codex 未生成回复文件，未发送消息")
            reply = output.read_text(encoding="utf-8").strip()
        return GenerationResult(reply)

"""Provider-neutral generation contract; providers never execute business actions."""

from dataclasses import dataclass, field
import hashlib
import json


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def scoped_key(account, *parts):
    """Unambiguous identity, including account and purpose, without exposing IDs."""
    return hashlib.sha256(
        compact_json([str(account), *map(str, parts)]).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Capabilities:
    structured_output: bool = False
    images: bool = False
    cache_key: bool = False


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    messages: tuple[dict, ...]
    images: tuple[str, ...] = ()
    response_schema: dict | None = None
    cache_key: str = ""
    max_output_tokens: int = 2048

    def as_text(self):
        return self.system + "\n对话数据（JSON）：\n" + compact_json(self.messages)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)

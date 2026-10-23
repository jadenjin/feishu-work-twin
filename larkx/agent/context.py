"""Bounded conversation storage and prompt budgeting shared by reply providers."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import time
import uuid
from weakref import WeakValueDictionary

from .contracts import compact_json


def estimate_tokens(text):
    # Portable estimate, not a model tokenizer or billing measurement.
    return (len(text.encode("utf-8")) + 2) // 3


def select_history(history, budget):
    selected = []
    for item in reversed(history):
        message = {"role": item["role"], "content": item.get("content", "")}
        cost = estimate_tokens(compact_json(message)) + 4
        if cost > budget:
            break
        selected.append(message)
        budget -= cost
    return list(reversed(selected))


@dataclass
class SessionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0


class ConversationStore:
    def __init__(self, directory, *, ttl=86400, cache_size=128):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self.states = WeakValueDictionary()

    def state(self, key):
        state = self.states.get(key)
        if state is None:
            state = SessionState()
            self.states[key] = state
        return state

    def path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def load(self, key):
        path = self.path(key)
        if not path.exists():
            self._cache.pop(key, None)
            return []
        stat = path.stat()
        if time.time() - stat.st_mtime > self.ttl:
            self._cache.pop(key, None)
            return []
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(key)
        if cached and cached[0] == stamp:
            self._cache.move_to_end(key)
            return [dict(item) for item in cached[1]]
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(history, list) or any(
                not isinstance(item, dict)
                or item.get("role") not in ("user", "assistant")
                or not isinstance(item.get("content"), str)
                or not isinstance(item.get("images", []), list)
                or any(not isinstance(p, str) for p in item.get("images", []))
                for item in history
            ):
                raise ValueError("invalid history")
        except (ValueError, UnicodeError):
            # Never silently discard a damaged conversation and invent continuity.
            raise RuntimeError("会话历史格式损坏，请清除该会话后重试") from None
        self._remember(key, stamp, history)
        return [dict(item) for item in history]

    def _remember(self, key, stamp, history):
        self._cache[key] = (stamp, history)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def save(self, key, history):
        path = self.path(key)
        pending = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            pending.write_text(compact_json(history), encoding="utf-8")
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
        stat = path.stat()
        self._remember(key, (stat.st_mtime_ns, stat.st_size), history)

    def reset(self, key):
        state = self.states.get(key)
        if state:
            state.revision += 1
        self._cache.pop(key, None)
        self.path(key).unlink(missing_ok=True)

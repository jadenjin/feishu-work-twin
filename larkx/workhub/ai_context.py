"""Reduce repeated message payloads without removing speaker or task identity."""

from html import unescape
import json
import re

from .control_policy import allowed_control_actions


def compact_records(rows, fields, *, budget, limit):
    """Keep identifiers intact; explicitly flag omitted records and field tails."""
    result, used, limited = [], 0, False
    for source in rows:
        item = {key: source.get(key) for key in fields if key in source}
        shortened = []
        for key, maximum in fields.items():
            value = item.get(key)
            if maximum and isinstance(value, str) and len(value) > maximum:
                item[key] = value[:maximum]
                shortened.append(key)
        if shortened:
            item["truncated_fields"] = shortened
            limited = True
        cost = len(json.dumps(item, ensure_ascii=False))
        if len(result) >= limit or used + cost > budget:
            limited = True
            break
        result.append(item)
        used += cost
    return result, limited


def message_time(message):
    value = float(message.get("create_time") or message.get("time") or 0)
    return value / 1000 if value > 100000000000 else value


def recent_reply_messages(messages, current, *, render=None):
    """Bound optional history, never the current incoming batch or stored records."""
    latest = message_time(current)
    selected, used, shortened = [], 0, False
    for source in reversed(messages):
        timestamp = message_time(source)
        if latest and timestamp and not 0 <= latest - timestamp <= 86400:
            continue
        item = dict(source)
        content = str(item.get("content") or "")
        if len(content) > 800:
            item["content"] = content[:800] + "[内容截断]"
            shortened = True
        cost = len(
            render(item)
            if render
            else json.dumps(
                {
                    k: item.get(k)
                    for k in ("msg_id", "sender_id", "sender", "time", "content")
                },
                ensure_ascii=False,
            )
        )
        if len(selected) >= 8 or used + cost > 3600:
            break
        selected.append(item)
        used += cost
    return list(reversed(selected)), {
        "recent_count": len(selected),
        "history_limited": shortened or len(selected) < len(messages),
        "history_window_hours": 24,
    }


def without_repeated_messages(context, body):
    # Only drop records demonstrably included in the XML body. Observations and
    # draft generation may not carry that body, so keep their recent messages.
    ids = {
        unescape(match[1])
        for match in re.findall(r"<message\s+id=([\"'])(.*?)\1>", body)
        if match[1]
    }
    return {
        **context,
        "recent_messages": [
            message
            for message in context.get("recent_messages", [])
            if str(message.get("msg_id") or "") not in ids
        ],
    }


def owner_query_history(jobs, revision, current_job=None, *, chat_ids=None):
    """Bounded read-only references; never inherit old mutation instructions."""
    history = []
    for job in jobs:
        if job["status"] != "done" or allowed_control_actions(job["input"]):
            continue
        if current_job and (
            job["id"] == current_job["id"]
            or job["created_at"] >= current_job["created_at"]
        ):
            continue
        try:
            result = json.loads(job["result"])
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("context_revision") != revision:
            continue
        if chat_ids is not None and sorted(
            result.get("context_chat_ids", [])
        ) != sorted(chat_ids):
            continue
        if result.get("actions") or not isinstance(result.get("answer"), str):
            continue
        history.append(
            {
                "question": job["input"][:500],
                "answer": result["answer"][:1500],
                "time": job["created_at"],
            }
        )
    return history[-3:]

"""Reduce repeated message payloads without removing speaker or task identity."""

from html import unescape
import re


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

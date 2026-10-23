"""Reduce repeated message payloads without removing speaker or task identity."""

from html import unescape
import json
import re

from .control_policy import allowed_control_actions


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


def owner_query_history(jobs, revision, current_job=None):
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

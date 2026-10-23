"""Resolve console targets locally before loading bounded model context."""

import re

from sqlalchemy import case, desc

from ..storage.models import Message
from .control_policy import control_instruction
from .ai_context import compact_records
from .models import Conversation, Persona, Task
from .store import record


def mentions(text, value):
    value = str(value or "").casefold()
    if not value:
        return False
    if value.isascii():
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", text
            )
        )
    return value in text


def build_control_context(store, text, allowed):
    instruction = control_instruction(text).casefold()
    recipient_operation = bool(allowed & {"send_message", "update_profile"})
    wants_tasks = "complete_task" in allowed or bool(
        re.search(
            r"待办|任务|事项|需要处理|需决定|工作安排|\b(?:tasks?|todos?)\b",
            instruction,
        )
    )
    wants_chats = not allowed and bool(
        re.search(r"会话|消息|聊天|联系人|同事|群聊", instruction)
    )
    wants_done = not allowed and bool(
        re.search(r"已完成|完成了|完成过|历史任务|\b(?:completed|done)\b", instruction)
    )
    global_chats = bool(
        re.search(r"所有|全部|各个|最近(?:的)?会话|会话进展|联系人列表", instruction)
    )
    with store.storage.Session() as session:
        # The complete directory stays local. No messages/profiles are loaded here.
        directory = [
            dict(row._mapping)
            for row in session.query(
                Conversation.chat_id,
                Conversation.name,
                Conversation.kind,
                Conversation.persona_key,
            )
            .order_by(desc(Conversation.updated_at), Conversation.chat_id)
            .all()
        ]
        matched = [
            row
            for row in directory
            if any(mentions(instruction, row[key]) for key in ("chat_id", "name"))
        ]
        # Prefer a complete longer name over its contained shorter name.
        matched = [
            row
            for row in matched
            if mentions(instruction, row["chat_id"])
            or not any(
                row["name"]
                and row["name"] != other["name"]
                and row["name"].casefold() in other["name"].casefold()
                for other in matched
            )
        ]
        ambiguous = any(
            not mentions(instruction, row["chat_id"])
            and sum(other["name"] == row["name"] for other in matched) > 1
            for row in matched
        )
        explicit_ids = [row for row in matched if mentions(instruction, row["chat_id"])]
        if explicit_ids:
            explicit_names = {row["name"] for row in explicit_ids}
            matched = [
                row
                for row in matched
                if row in explicit_ids or row["name"] not in explicit_names
            ]
            ambiguous = any(
                sum(other["name"] == row["name"] for other in matched) > 1
                and row["name"] not in explicit_names
                for row in matched
            )
        unresolved = (
            ambiguous
            or len(matched) > 4
            or (
                not matched
                and (recipient_operation or (wants_chats and not global_chats))
            )
        )
        selected = [] if unresolved else matched
        chat_ids = [row["chat_id"] for row in selected]
        profiles = []
        if selected and (len(selected) == 1 or "update_profile" in allowed):
            profiles = [
                record(row)
                for row in session.query(Persona)
                .filter(Persona.key.in_([row["persona_key"] for row in selected]))
                .all()
            ]
        profiles, profiles_limited = compact_records(
            profiles,
            {
                "key": 0,
                "name": 120,
                "role": 160,
                "description": 400,
                "reply_style": 300,
            },
            budget=1800,
            limit=4,
        )

        conversations = [
            {k: row[k] for k in ("chat_id", "name", "kind")} for row in selected
        ]
        # Multiple-recipient sends must not copy one recipient's history to another.
        if len(selected) == 1:
            current = session.get(Conversation, chat_ids[0])
            recent = (
                session.query(Message)
                .filter_by(chat_id=chat_ids[0])
                .order_by(desc(Message.create_time), desc(Message.id))
                .limit(4)
                .all()
            )

            def message_time(row):
                value = float(row.create_time or 0)
                return value / 1000 if value > 100000000000 else value

            if recent:
                cutoff = message_time(recent[0]) - 86400
                recent = [row for row in recent if message_time(row) >= cutoff]
            messages, _ = compact_records(
                [
                    {
                        "sender": row.sender_name,
                        "direction": row.direction,
                        "time": row.create_time,
                        "content": row.content,
                    }
                    for row in reversed(recent)
                ],
                {"sender": 80, "direction": 0, "time": 0, "content": 400},
                budget=2200,
                limit=4,
            )
            conversations[0].update(
                overview=(current.overview or "")[:400],
                recent_messages=messages,
                context_limited=True,
            )
        elif not allowed and not matched and wants_chats and not unresolved:
            recent_chats = (
                session.query(Conversation)
                .order_by(desc(Conversation.updated_at))
                .limit(11)
                .all()
            )
            conversations, _ = compact_records(
                [record(row) for row in recent_chats],
                {
                    "chat_id": 0,
                    "name": 100,
                    "kind": 0,
                    "overview": 240,
                },
                budget=3000,
                limit=10,
            )

        task_query = session.query(Task)
        if chat_ids:
            task_query = task_query.filter(Task.chat_id.in_(chat_ids))
        task_ids = [
            int(value)
            for value in re.findall(
                r"(?:#|(?:待办|任务|\btask\b|\btodo\b)\s*(?:编号|id|#)?\s*)(\d+)",
                instruction,
            )
        ]
        wants_tasks = wants_tasks or bool(task_ids)
        matching = []
        if task_ids:
            task_query = task_query.filter(Task.id.in_(task_ids))
        else:
            task_query = task_query.filter(
                Task.status == "done" if wants_done else Task.status != "done"
            )
            if wants_tasks:
                # Titles/IDs are searched locally; unrelated task bodies stay out.
                titles = task_query.with_entities(Task.id, Task.title).all()
                matching = [
                    row.id for row in titles if mentions(text.casefold(), row.title)
                ]
                if matching:
                    task_query = task_query.filter(Task.id.in_(matching))
        tasks, total, tasks_limited = [], 0, False
        if wants_tasks and not unresolved:
            total = task_query.count()
            rows = (
                task_query.order_by(
                    case((Task.status == "needs_attention", 0), else_=1),
                    case((Task.priority == "high", 0), else_=1),
                    desc(Task.updated_at),
                    desc(Task.id),
                )
                .limit(13)
                .all()
            )
            tasks, tasks_limited = compact_records(
                [record(row) for row in rows],
                {
                    "id": 0,
                    "title": 256,
                    "details": 240,
                    "status": 0,
                    "priority": 0,
                    "chat_id": 0,
                    "anchor": 0,
                    "due_at": 80,
                },
                budget=3600,
                limit=12,
            )
            tasks_limited = tasks_limited or len(tasks) < total
            if "complete_task" in allowed and (
                (not task_ids and not matching and total != 1)
                or (task_ids and len(tasks) != len(set(task_ids)))
            ):
                unresolved = True
        candidates, _ = compact_records(
            (matched or directory)
            if unresolved and (recipient_operation or wants_chats)
            else [],
            {
                "chat_id": 0,
                "name": 120,
                "kind": 0,
            },
            budget=1400,
            limit=10,
        )
    return {
        "conversations": conversations,
        "personas": profiles,
        "tasks": tasks,
        "chat_ids": chat_ids,
        "profile_keys": [p["key"] for p in profiles],
        "task_total": total,
        "scope": {
            "chat_ids": chat_ids,
            "selection_required": unresolved,
            "recipient_candidates": candidates,
            "candidate_list_limited": unresolved
            and len(matched or directory) > len(candidates),
            "tasks_status": "explicit_ids"
            if task_ids
            else "done"
            if wants_done
            else "unfinished",
            "tasks_limited": tasks_limited,
            "profiles_limited": profiles_limited,
            "conversations_limited": bool(wants_chats and not matched),
            "conversation_total": len(directory)
            if wants_chats and not matched
            else len(selected),
            "tasks_requested": wants_tasks,
            "conversations_requested": bool(wants_chats or matched),
        },
    }


def needs_work_context(text, projects):
    query = text.casefold()
    return bool(
        re.search(
            r"工作资料|工作内容|职责|负责|项目|代码|源码|实现|配置|接口|文档|技术|刚才|上面|继续",
            query,
        )
    ) or any(
        mentions(query, name)
        for project in projects
        for name in [project["name"], *project.get("aliases", [])]
    )


def compact_work_context(work):
    """The owner console uses short evidence, not complete stored profiles."""
    result = dict(work)
    result["projects"], profiles_limited = compact_records(
        work.get("projects", []),
        {
            "id": 0,
            "name": 120,
            "profile": 500,
            "learned_at": 0,
            "profile_truncated": 0,
        },
        budget=1800,
        limit=3,
    )
    searches = []
    for search in work.get("searches", [])[:2]:
        snippets, limited = compact_records(
            search.get("snippets", []),
            {
                "path": 240,
                "line": 0,
                "text": 700,
                "modified_at": 0,
            },
            budget=1800,
            limit=2,
        )
        searches.append(
            {
                **search,
                "snippets": snippets,
                "limited": bool(search.get("limited") or limited),
            }
        )
    result["searches"] = searches
    result["context_limited"] = profiles_limited or any(s["limited"] for s in searches)
    return result

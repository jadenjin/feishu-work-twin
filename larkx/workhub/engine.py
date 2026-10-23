import asyncio
import json
from collections import defaultdict
from copy import deepcopy
from .store import DEFAULT_RULES
from .ai_context import without_repeated_messages
from .control_policy import allowed_control_actions
from .prompts import analysis_prompt, control_prompt, polish_prompt


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
TASK_SCHEMA = obj(
    {
        "task_id": {"type": "integer", "minimum": 0},
        "title": TEXT,
        "details": TEXT,
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "due_at": TEXT,
    }
)
WORK_SCHEMA = obj(
    {
        "should_reply": {"type": "boolean"},
        "reply_reason": TEXT,
        "reply": TEXT,
        "reply_to_message_id": TEXT,
        "mention_user_id": TEXT,
        "overview": TEXT,
        "facts": {"type": "array", "items": TEXT},
        "tasks": {"type": "array", "items": TASK_SCHEMA},
        "needs_attention": {"type": "boolean"},
        "attention_reason": TEXT,
        "attention_task_id": {"type": "integer", "minimum": 0},
    }
)
CONTROL_ACTION = obj(
    {
        "type": {
            "type": "string",
            "enum": ["send_message", "create_task", "complete_task", "update_profile"],
        },
        "target": TEXT,
        "chat_id": TEXT,
        "content": TEXT,
        "task_id": {"type": "integer"},
        "title": TEXT,
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "role": TEXT,
        "description": TEXT,
        "profile_fields": {
            "type": "array",
            "items": {"type": "string", "enum": ["role", "description"]},
        },
    }
)
CONTROL_SCHEMA = obj(
    {"answer": TEXT, "actions": {"type": "array", "items": CONTROL_ACTION}}
)


class WorkEngine:
    def __init__(self, runtime):
        self.runtime = runtime
        self.analysis_locks = defaultdict(asyncio.Lock)

    async def process(self, session_key, msg, body, images, reply_fn, send_reply=True):
        # Reply and observation paths must see the previous analysis' committed tasks.
        async with self.analysis_locks[str(msg["chat_id"])]:
            return await self._process(
                session_key, msg, body, images, reply_fn, send_reply
            )

    async def _process(self, session_key, msg, body, images, reply_fn, send_reply=True):
        store = self.runtime.hub
        chat_id = str(msg["chat_id"])
        handoff = store.handoff(chat_id)
        human_paused = store.human_paused(chat_id) or bool(msg.get("_human_handled"))
        ctx = store.context(
            str(msg["chat_id"]),
            str(msg.get("from_id") or ""),
            anchor=msg.get("anchor") or "",
        )
        if msg.get("chat_type") in (2, 3):
            names = {
                str(row.get("sender_id") or ""): row.get("sender") or ""
                for row in ctx.get("recent_messages", [])
                if row.get("sender_id")
            }
            for member in msg.get("_batch") or [msg]:
                user_id = str(member.get("from_id") or "")
                if user_id:
                    names[user_id] = member.get("_sender_name") or names.get(
                        user_id, ""
                    )
            profiles = store.user_profiles(names)
            ctx["participants"] = {
                user_id: profiles.get(user_id) or {"external_id": user_id, "name": name}
                for user_id, name in names.items()
            }
            ctx["self_user_id"] = str(
                getattr(getattr(self.runtime.client, "auth", None), "user_id", "")
            )
        ctx = without_repeated_messages(ctx, body)
        mode = ctx.get("persona", {}).get("mode", "auto")
        paused = store.setting("automation_enabled", "true") != "true"
        my_id = str(getattr(getattr(self.runtime.client, "auth", None), "user_id", ""))
        ctx["self_user_id"] = my_id
        ctx["analysis_mode"] = "reply" if send_reply else "observe"
        required_reply = bool(
            send_reply
            and msg.get("chat_type") in (2, 3)
            and store.group_reply_policy(chat_id) == "all"
            and any(
                not member.get("_reply_context_only")
                and str(member.get("from_id") or "") != my_id
                and (
                    (member.get("at_me") and not member.get("at_all"))
                    or (my_id and my_id in member.get("at_user_ids", []))
                )
                for member in msg.get("_batch") or [msg]
            )
        )
        prompt = analysis_prompt(
            store.setting("own_rules", DEFAULT_RULES),
            ctx,
            body,
            casual_group=msg.get("chat_type") in (2, 3)
            and store.group_reply_policy(chat_id) == "all",
            required_reply=required_reply,
        )
        allowed_task_ids = [task["id"] for task in ctx["tasks"]]
        schema = deepcopy(WORK_SCHEMA)
        schema["properties"]["attention_task_id"]["enum"] = [0] + allowed_task_ids
        schema["properties"]["tasks"]["items"]["properties"]["task_id"]["enum"] = [
            0
        ] + allowed_task_ids
        if required_reply:
            schema["properties"]["should_reply"]["enum"] = [True]
            schema["properties"]["reply"]["minLength"] = 1
        async with self.runtime.model_slots:
            raw = await self.runtime.generate(
                "work:" + json.dumps([session_key, chat_id, msg.get("anchor") or ""]),
                prompt,
                image_paths=images,
                response_schema=schema,
            )
        result = json.loads(raw)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("tasks"), list)
            or not isinstance(result.get("should_reply"), bool)
        ):
            raise ValueError("工作分析返回格式不完整")
        if not send_reply:
            result["should_reply"] = False
            result["reply"] = ""
        if (
            my_id
            and str(msg.get("from_id") or "") == my_id
            and not result.get("tasks")
            and not result.get("attention_task_id")
        ):
            # Own clarifications without a new task cannot create a missing-info alert.
            result["needs_attention"] = False
        if (
            ctx["overview"]
            and not result.get("facts")
            and not result.get("tasks")
            and not result.get("needs_attention")
        ):
            result["overview"] = ctx["overview"]
        reply = str(result.get("reply") or "").strip()
        if required_reply:
            result["should_reply"] = True
            if not reply or reply == "[[NO_REPLY]]":
                reply = "我在。"
            result["reply"] = reply
        if not result["should_reply"]:
            reply = ""
            result["reply"] = ""
        elif not reply or reply == "[[NO_REPLY]]":
            raise ValueError("AI 判断需要回复，但没有提供有效回复")
        targets = {}
        if reply and msg.get("chat_type") in (2, 3):
            candidates = store.reply_context(msg) + list(msg.get("_batch") or [msg])
            message_ids = {str(m.get("msg_id") or "") for m in candidates} - {""}
            user_ids = {str(m.get("from_id") or "") for m in candidates} - {"", my_id}
            for key, allowed in (
                ("reply_to_message_id", message_ids),
                ("mention_user_id", user_ids),
            ):
                value = result.get(key) or ""
                if not isinstance(value, str) or (value and value not in allowed):
                    raise ValueError("AI 选择的引用/@对象不在当前对话中")
                if value:
                    targets[key] = value
        store.apply_analysis(msg, result, allowed_task_ids=allowed_task_ids)
        # Owner can pause or switch to draft while a model call is in progress.
        for member in msg.get("_batch") or [msg]:
            member_ctx = store.context(chat_id, str(member.get("from_id") or ""))
            if (
                member.get("_human_handled")
                or member_ctx.get("sender", {}).get("mode") == "off"
            ):
                send_reply = False
        latest_ctx = store.context(chat_id, str(msg.get("from_id") or ""))
        modes = {
            latest_ctx.get("persona", {}).get("mode", "auto"),
            latest_ctx.get("sender", {}).get("mode", "auto"),
        }
        mode = "off" if "off" in modes else "draft" if "draft" in modes else "auto"
        paused = store.setting("automation_enabled", "true") != "true"
        latest_handoff = store.handoff(chat_id)
        human_paused = (
            human_paused
            or store.human_paused(chat_id)
            or latest_handoff.get("generation", 0) != handoff.get("generation", 0)
        )
        # A reply queued before the owner took over must never be sent later.
        message_at = float(msg.get("create_time") or 0)
        if message_at > 100000000000:
            message_at /= 1000
        human_paused = human_paused or (
            message_at > 0 and message_at <= latest_handoff.get("last_manual", 0)
        )
        if human_paused:
            send_reply = False
        if msg.get("chat_type") in (2, 3):
            # Re-check after generation: switching a group off cancels pending replies.
            triggers = [
                m
                for m in (msg.get("_batch") or [msg])
                if not m.get("_reply_context_only")
            ]
            policy = store.group_reply_policy(chat_id)
            my_id = str(
                getattr(getattr(self.runtime.client, "auth", None), "user_id", "")
            )
            allowed = (
                policy == "all"
                or (policy == "at_all" and any(m.get("at_all") for m in triggers))
                or (
                    policy == "at_me"
                    and any(
                        (m.get("at_me") and not m.get("at_all"))
                        or my_id in m.get("at_user_ids", [])
                        for m in triggers
                    )
                )
            )
            if not allowed:
                send_reply = False
        if reply and send_reply and mode == "auto" and not paused:
            await reply_fn(
                str(msg["chat_id"]), reply, msg.get("anchor") or None, **targets
            )
        elif reply and send_reply and mode != "off" and (mode == "draft" or paused):
            store.save_reply_review(msg, reply)
        self.runtime.last_analysis = str(msg["chat_id"])
        return result

    async def control(self, text, job_id):
        store = self.runtime.hub
        allowed = allowed_control_actions(text)
        conversations, personas = store.conversations(), store.personas()
        all_tasks = store.tasks()
        tasks = (
            [t for t in all_tasks if t["status"] != "done"]
            + [t for t in all_tasks if t["status"] == "done"]
        )[:80]
        chat_ids = {c["chat_id"] for c in conversations}
        profiles = {p["key"] for p in personas}
        task_by_id = {t["id"]: t for t in tasks}
        prompt = control_prompt(text, conversations, personas, tasks, allowed)
        response_schema = deepcopy(CONTROL_SCHEMA)
        action_schema = response_schema["properties"]["actions"]
        action_schema["maxItems"] = 100 if allowed else 0
        fields = action_schema["items"]["properties"]
        if allowed:
            fields["type"]["enum"] = sorted(allowed)
        # TEXT schemas are shared; replace the field instead of narrowing all strings.
        fields["chat_id"] = {**fields["chat_id"], "enum": [""] + sorted(chat_ids)}
        fields["target"] = {**fields["target"], "enum": [""] + sorted(profiles)}
        fields["task_id"] = {**fields["task_id"], "enum": [0] + list(task_by_id)}
        async with self.runtime.model_slots:
            raw = await self.runtime.generate(
                "owner-control", prompt, response_schema=response_schema
            )
        plan = json.loads(raw)
        if not isinstance(plan, dict) or not isinstance(plan.get("actions"), list):
            raise ValueError("控制台返回了无效的执行计划")
        actions = plan["actions"]
        if len(actions) > 100:
            raise ValueError("一次指令最多执行 100 个动作，请分批安排")
        results = []
        for index, action in enumerate(actions):
            kind = action.get("type") if isinstance(action, dict) else "invalid"
            try:
                if kind not in allowed:
                    raise ValueError(
                        "当前指令没有明确要求发送，查询、起草或不发送指令已阻止发送"
                        if kind == "send_message"
                        else "当前指令未明确要求此操作，已阻止修改"
                    )
                if kind == "send_message":
                    if action.get("chat_id") not in chat_ids:
                        raise ValueError("收件人不在本次提供的会话中，请明确选择")
                    task_id = action.get("task_id", 0)
                    if type(task_id) is not int or (
                        task_id and task_id not in task_by_id
                    ):
                        raise ValueError("关联任务不在本次提供的任务中")
                    task = task_by_id.get(task_id)
                    data = await self.runtime.send(
                        action["chat_id"],
                        action["content"],
                        f"{job_id}:{index}",
                        anchor=task["anchor"]
                        if task and task["chat_id"] == action["chat_id"]
                        else "",
                        manual=True,
                    )
                elif kind == "create_task":
                    if action.get("chat_id") and action["chat_id"] not in chat_ids:
                        raise ValueError("关联会话不在本次提供的会话中")
                    data = store.create_task(
                        {
                            "title": action["title"],
                            "details": action["content"],
                            "chat_id": action["chat_id"],
                            "priority": action["priority"],
                        }
                    )
                elif kind == "complete_task":
                    if (
                        type(action.get("task_id")) is not int
                        or action["task_id"] not in task_by_id
                    ):
                        raise ValueError("要完成的任务不在本次提供的任务中")
                    data = store.update_task(action["task_id"], {"status": "done"})
                elif kind == "update_profile":
                    if action.get("target") not in profiles:
                        raise ValueError("角色档案不在本次提供的档案中")
                    fields = action.get("profile_fields")
                    if (
                        not isinstance(fields, list)
                        or not fields
                        or any(f not in ("role", "description") for f in fields)
                    ):
                        raise ValueError("请明确要修改的角色字段")
                    if any(not isinstance(action.get(f), str) for f in fields):
                        raise ValueError("角色字段必须是文字")
                    data = store.update_persona(
                        action["target"], {f: action[f] for f in fields}
                    )
                ok = kind != "send_message" or data.get("status") == "sent"
                item = {"type": kind, "ok": ok, "data": data}
                if not ok:
                    item["error"] = data.get("error") or "发送结果未确认"
                results.append(item)
            except Exception as error:
                results.append({"type": kind, "ok": False, "error": str(error)})
        return {"answer": plan.get("answer", ""), "actions": results}

    async def polish_task(self, task, text, instruction=""):
        return await self.polish_reply(task["chat_id"], text, instruction, task)

    async def polish_reply(self, chat_id, text, instruction="", task=None):
        store = self.runtime.hub
        ctx = store.context(chat_id) if chat_id else {}
        prompt = polish_prompt(
            store.setting("own_rules", DEFAULT_RULES),
            ctx,
            text,
            instruction,
            task=task,
        )
        async with self.runtime.model_slots:
            session = (
                "task-draft:" + str(task["id"])
                if task
                else "conversation-draft:" + chat_id
            )
            raw = await self.runtime.generate(
                session, prompt, response_schema=obj({"reply": TEXT})
            )
        result = json.loads(raw)
        if not isinstance(result.get("reply"), str) or not result["reply"].strip():
            raise ValueError("AI 没有生成有效草稿，请重试")
        result["reply"] = result["reply"].strip()
        return result

import asyncio
import json
import os
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from ..config import load_config
from ..workhub.ai_context import message_time, recent_reply_messages
from .contracts import scoped_key

INSTRUCTIONS = "以上是飞书会话里新到的一条消息(XML 里有会话/发送者/内容)。你是这个数字人,完整对话历史你都有。如果对方也是机器人且只是互相客套寒暄,直接结束不要再接话。"
OUTPUT_RULES = "不需要回复就直接结束,不要解释、不要复述消息。"


def _ts(msg: dict) -> str:
    t = msg.get("create_time")
    if not t:
        return ""
    try:
        timestamp = float(t)
        if timestamp > 100000000000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(t)


def build_xml_message(m: dict) -> str:
    lines = [f'<message id={quoteattr(str(m.get("msg_id") or ""))}>']
    lines.append(
        f"  <chat id={quoteattr(str(m.get('chat_id')))} name={quoteattr(str(m.get('_chat_name') or ''))} type={quoteattr(str(m.get('chat_type_name') or ''))} anchor={quoteattr(str(m.get('anchor') or ''))}/>"
    )
    lines.append(
        f"  <sender id={quoteattr(str(m.get('from_id')))} name={quoteattr(str(m.get('_sender_name') or ''))}/>"
    )
    if m.get("at_me"):
        lines.append("  <at_me>true</at_me>")
    lines.append(f"  <time>{escape(_ts(m))}</time>")
    lines.append(f"  <content>{escape(str(m.get('content') or ''))}</content>")
    lines.append("</message>")
    return "\n".join(lines)


class SessionWorker:
    def __init__(self, dispatcher, session_key: str):
        self.dispatcher = dispatcher
        self.session_key = session_key
        self.queue = asyncio.Queue()
        self.collecting = False
        self.task = asyncio.create_task(self._run())

    async def collect_batch(self, first):
        # Wait for a quiet interval, not a fixed interval after the first sentence.
        workflow = getattr(self.dispatcher, "workflow", None)
        if workflow:
            timing = workflow.runtime.hub.batch_timing()
            delay, maximum = timing["batch_delay"], timing["batch_max_wait"]
        else:
            delay = max(
                0.01, min(float(os.environ.get("LARKX_BATCH_DELAY", "1.5")), 30)
            )
            maximum = max(
                delay, min(float(os.environ.get("LARKX_BATCH_MAX_WAIT", "5")), 60)
            )
        deadline = asyncio.get_running_loop().time() + maximum
        batch = [first]
        self.collecting = True
        try:
            while len(batch) < 20:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(
                        await asyncio.wait_for(self.queue.get(), min(delay, remaining))
                    )
                except asyncio.TimeoutError:
                    break
        finally:
            self.collecting = False
        return batch

    async def _run(self):
        while True:
            msg = await self.queue.get()
            try:
                if msg.get("_reply_context_only"):
                    continue
                if self.dispatcher.backend.returns_reply:
                    batch = await self.collect_batch(msg)
                    if len(batch) > 1:
                        msg = dict(batch[-1], _batch=batch)
                await self.dispatcher.handle_message(self.session_key, msg)
            except Exception as e:
                logger.error(f"处理消息异常 [{self.session_key}]: {e}")
                workflow = getattr(self.dispatcher, "workflow", None)
                if workflow:
                    workflow.runtime.processing.fail(
                        msg.get("_batch") or [msg], str(e)
                    )


class AgentDispatcher:
    def __init__(self, backend, reply_fn, image_prepare=None, workflow=None):
        self.cfg = load_config()
        self.backend = backend
        self.reply_fn = reply_fn
        self.image_prepare = image_prepare
        self.workflow = workflow
        self.workers = {}

    def session_key_of(self, msg: dict) -> str:
        # Legacy 'global' no longer shares history across unrelated people.
        # A group thread is intentionally shared by its participants.
        anchor = (
            "" if not self.workflow and self.cfg["context_scope"] == "chat"
            else msg.get("anchor") or ""
        )
        return scoped_key(
            msg.get("_account_id") or "local-owner", "chat", msg.get("chat_id"), anchor
        )

    def should_handle(self, msg: dict, my_user_id: str) -> bool:
        if str(msg.get("from_id")) == str(my_user_id):
            return False
        t = self.cfg.get("triggers") or {}
        content = (msg.get("content") or "").strip()
        chat_id = str(msg.get("chat_id"))
        sender_id = str(msg.get("from_id"))
        type_name = {1: "p2p", 2: "group", 3: "topic_group"}.get(
            msg.get("chat_type"), ""
        )
        if t.get("prefix") and (not content.startswith(t["prefix"])):
            return False
        chat_types = t.get("chat_types") or []
        if chat_types and type_name not in chat_types:
            return False
        include = [str(x) for x in t.get("include_chats") or []]
        if include and chat_id not in include:
            return False
        exclude = [str(x) for x in t.get("exclude_chats") or []]
        if exclude and chat_id in exclude:
            return False
        senders = [str(x) for x in t.get("include_senders") or []]
        if senders and sender_id not in senders:
            return False
        keywords = t.get("keywords") or []
        if keywords and (not any((kw in content for kw in keywords))):
            return False
        if self.workflow and type_name in ("group", "topic_group"):
            policy = self.workflow.runtime.hub.group_reply_policy(chat_id)
            if policy == "off":
                return False
            if policy == "at_all" and not msg.get("at_all"):
                return False
            if policy == "at_me" and not (
                str(my_user_id) in msg.get("at_user_ids", [])
                or (msg.get("at_me") and not msg.get("at_all"))
            ):
                return False
        elif (
            t.get("group_at_only")
            and type_name in ("group", "topic_group")
            and (not msg.get("at_me"))
        ):
            return False
        return True

    async def submit(
        self,
        msg: dict,
        chat_name: str = "",
        sender_name: str = "",
        my_user_id: str = "",
    ):
        msg = dict(msg, _account_id=str(my_user_id or "local-owner"))
        if not self.should_handle(msg, my_user_id):
            # A non-mention can complete an already-triggered group request during
            # its quiet interval, but must never start a new automatic reply.
            worker = self.workers.get(self.session_key_of(msg))
            followup = (
                self.workflow
                and msg.get("chat_type") in (2, 3)
                and self.workflow.runtime.hub.group_reply_policy(msg["chat_id"])
                in ("at_me", "at_all")
                and worker
                and (worker.collecting or not worker.queue.empty())
                and self.should_handle(
                    dict(msg, at_me=True, at_all=True, at_user_ids=[str(my_user_id)]),
                    my_user_id,
                )
            )
            if not followup:
                return
            msg = dict(msg, _reply_context_only=True)
        cmd, remainder = (
            (None, None)
            if self.workflow
            else self._parse_command(msg.get("content") or "")
        )
        key = self.session_key_of(msg)
        chat_id = str(msg["chat_id"])
        root_id = (msg.get("anchor") or "") or None
        if cmd == "clear":
            self._drain_queue(key)
            self.backend.reset_session(key)
            if not remainder:
                await self.reply_fn(chat_id, "✅ 已开启新会话", root_id)
                return
        elif cmd == "stop":
            n = self._drain_queue(key)
            if not remainder:
                await self.reply_fn(
                    chat_id,
                    f"⏹ 已清空 {n} 条排队消息" if n else "⏹ 队列已清空",
                    root_id,
                )
                return
        msg = dict(msg)
        if cmd:
            msg["content"] = remainder
        msg["_chat_name"] = chat_name
        msg["_sender_name"] = sender_name
        if key not in self.workers:
            self.workers[key] = SessionWorker(self, key)
        await self.workers[key].queue.put(msg)

    def _parse_command(self, content: str):
        t = self.cfg.get("triggers") or {}
        prefix = t.get("prefix") or ""
        c = content.strip()
        if prefix and c.startswith(prefix):
            c = c[len(prefix) :].lstrip()
        for cmd in ("/clear", "/stop"):
            if c == cmd:
                return cmd[1:], ""
            if (
                c.startswith(cmd)
                and len(c) > len(cmd)
                and (c[len(cmd)] in (" ", "\t", "\n"))
            ):
                return cmd[1:], c[len(cmd) :].strip()
        return None, None

    def _drain_queue(self, key) -> int:
        w = self.workers.get(key)
        if not w:
            return 0
        n = 0
        while True:
            try:
                w.queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def handle_message(self, session_key: str, msg: dict):
        batch = msg.get("_batch") or [msg]
        body = "\n".join(build_xml_message(item) for item in batch)
        current_body = body
        context = []
        if self.workflow:
            ids = {str(item.get("msg_id")) for item in batch}
            context = await asyncio.to_thread(
                self.workflow.runtime.hub.reply_context, msg
            )
            context = [item for item in context if str(item.get("msg_id")) not in ids]
            context, history_scope = recent_reply_messages(
                context, msg, render=build_xml_message
            )
            # The engine must not reintroduce discarded history from its DB snapshot.
            msg = dict(
                msg,
                _reply_history_scope=history_scope,
                _reply_context_ids=[str(item.get("msg_id") or "") for item in context],
            )
            if context:
                body = (
                    "同一会话/话题的最近背景消息（仅作上下文，不独立触发回复）：\n"
                    + "\n".join(build_xml_message(item) for item in context)
                    + "\n本次连续收到的消息（请合并理解）：\n"
                    + body
                )
        image_paths = []
        image_sources = []
        if self.backend.returns_reply and self.image_prepare:
            # Include images immediately preceding a standalone @mention too.
            latest_time = message_time(msg)
            preceding_images = []
            image_window = (
                1800
                if any(
                    word
                    in current_body
                    + "\n"
                    + "\n".join(str(item.get("content") or "") for item in context[-2:])
                    for word in ("截图", "图片", "上面", "照片")
                )
                else 180
            )
            for item in context:
                timestamp = message_time(item)
                if latest_time and 0 <= latest_time - timestamp <= image_window:
                    preceding_images.append(item)
            for item in batch + list(reversed(preceding_images)):
                if item.get("msg_type") in (2, 5):
                    source = {"msg_id": str(item.get("msg_id") or "")}
                    if len(image_paths) >= 3:
                        image_sources.append(
                            {**source, "status": "未加载，已达到三张图片上限"}
                        )
                        continue
                    try:
                        prepared = await self.image_prepare(item)
                        paths = prepared if isinstance(prepared, list) else [prepared]
                        for position, path in enumerate(paths, 1):
                            if not path:
                                continue
                            if path not in image_paths and len(image_paths) < 3:
                                image_paths.append(path)
                            image_sources.append(
                                {
                                    **source,
                                    "position_in_message": position,
                                    "attachment_index": image_paths.index(path) + 1
                                    if path in image_paths
                                    else None,
                                    "status": "已附带可读取图片"
                                    if path in image_paths
                                    else "未加载，已达到三张图片上限",
                                }
                            )
                    except Exception as e:
                        logger.warning(
                            f"图片下载失败 [{session_key}]: {type(e).__name__}"
                        )
                        image_sources.append({**source, "status": "下载失败"})
        if image_sources:
            body += (
                "\n本次图片附件与消息对应关系（attachment_index 从 1 起，与附件顺序一致）：\n"
                + json.dumps(image_sources, ensure_ascii=False)
                + "\n请先读取已附带图片，再结合对应消息回答。[文档]标记和历史中看不到旧图的说法"
                "不代表本次图片不可见。区分图中可识别的内容与图中没有的日期、排期、承诺；"
                "仅对本次问题需要但未加载/下载失败的图片说明缺项，不要求重发已可读的图片。"
            )
        chat_id = str(msg["chat_id"])
        root_id = (msg.get("anchor") or "") or None
        reply_cmd = f'lark send {chat_id} "<回复内容>"'
        if self.workflow:
            await self.workflow.process(
                session_key, msg, body, image_paths, self.reply_fn
            )
            return
        if root_id:
            reply_cmd += f" --root {root_id}"
        if self.backend.returns_reply:
            rules = (
                "此消息已经通过自动回复触发规则，必须给出简短自然的回复正文。"
                "表情也要回应；无法看到表情内容时不要猜测其含义。"
                "不要输出 [[NO_REPLY]]，不要执行发送命令。"
            )
        else:
            rules = f"要回复就执行: {reply_cmd}\n" + OUTPUT_RULES
        if self.cfg.get("system_prompt"):
            instructions = self.cfg["system_prompt"] + "\n" + rules
        else:
            instructions = INSTRUCTIONS + "\n" + rules
        prompt = body + "\n\n" + instructions
        kwargs = {"chat_id": chat_id, "anchor": msg.get("anchor") or ""}
        if self.backend.returns_reply:
            kwargs["require_reply"] = True
            kwargs["image_paths"] = image_paths
        out = await self.backend.run(session_key, prompt, **kwargs)
        if out:
            if self.backend.returns_reply:
                await self.reply_fn(chat_id, out, root_id)
                logger.info(f"自动回复已提交 [{session_key}]")
            else:
                logger.debug(f"[{session_key}] agent 输出: {out[:200]}")

"""Pending message work survives restart without replaying external actions."""

from collections import defaultdict
import hashlib
import json

from .models import Setting, Task


class MessageProcessing:
    prefix = "pending_message:"

    def __init__(self, storage):
        self.storage = storage

    def key(self, msg):
        source = json.dumps([str(msg["chat_id"]), str(msg.get("msg_id") or "")])
        return self.prefix + hashlib.sha256(source.encode()).hexdigest()

    @staticmethod
    def source(msg):
        return {
            "chat_id": str(msg["chat_id"]),
            "anchor": msg.get("anchor") or "",
            "msg_id": str(msg.get("msg_id") or ""),
            "content": str(msg.get("content") or "")[:500],
        }

    def enqueue(self, msg):
        if not msg.get("msg_id"):
            return
        with self.storage.Session() as session:
            key = self.key(msg)
            if not session.get(Setting, key):
                session.add(Setting(key=key, value=json.dumps(self.source(msg))))
                session.commit()

    def finish(self, batch):
        keys = [self.key(msg) for msg in batch if msg.get("msg_id")]
        if keys:
            with self.storage.Session() as session:
                session.query(Setting).filter(Setting.key.in_(keys)).delete(
                    synchronize_session=False
                )
                session.commit()

    @staticmethod
    def _review(session, sources, title, reason):
        groups = defaultdict(list)
        for msg in sources:
            groups[(msg["chat_id"], msg["anchor"])].append(msg)
        for (chat_id, anchor), messages in groups.items():
            detail = reason[:1000] + "\n\n相关消息：\n" + "\n".join(
                f"{m['msg_id']}：{m['content'][:180]}" for m in messages[:20]
            )
            if len(messages) > 20:
                detail += f"\n另有 {len(messages) - 20} 条，请查看同一话题的消息记录。"
            session.add(
                Task(
                    title=title,
                    details=detail,
                    chat_id=chat_id,
                    anchor=anchor,
                    origin_msg_id=messages[-1]["msg_id"],
                    status="needs_attention",
                    priority="high",
                )
            )

    def fail(self, batch, reason):
        keys = [self.key(msg) for msg in batch if msg.get("msg_id")]
        with self.storage.Session() as session:
            rows = session.query(Setting).filter(Setting.key.in_(keys)).all()
            sources = (
                [json.loads(row.value) for row in rows]
                if rows else [self.source(msg) for msg in batch]
            )
            self._review(session, sources, "消息处理异常，请核对", reason)
            for row in rows:
                session.delete(row)
            session.commit()

    def recover(self):
        with self.storage.Session() as session:
            rows = session.query(Setting).filter(
                Setting.key.startswith(self.prefix, autoescape=True)
            ).all()
            self._review(
                session,
                [json.loads(row.value) for row in rows],
                "服务中断，有消息待核对",
                "消息已入队，但处理完成状态未保存。部分分析或发送可能已经完成，"
                "请先核对会话、待办和执行记录；不会自动补发回复。",
            )
            # Creating reviews and consuming pending entries is one transaction.
            for row in rows:
                session.delete(row)
            session.commit()

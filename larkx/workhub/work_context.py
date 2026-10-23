"""Bounded, read-only work evidence for the owner console and local AI tools.

No model call is needed here: callers can select an exact project and search its
current files. Recipient scope always intersects the owner's explicit bindings.
"""

import asyncio
from datetime import datetime, timezone
import re

from .knowledge import search_project


def project_catalog(projects, profile_limit=800):
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "aliases": p.get("aliases", []),
            "profile": p.get("profile", "")[:profile_limit],
            "profile_truncated": len(p.get("profile", "")) > profile_limit,
            "learned_at": p.get("learned_at", ""),
        }
        for p in projects
    ]


class WorkContext:
    def __init__(self, runtime):
        self.runtime = runtime

    def projects(self, chat_id=""):
        projects = self.runtime.knowledge.state()["projects"]
        return [p for p in projects if not chat_id or str(chat_id) in p["chat_ids"]]

    async def read(self, query="", project_id="", chat_id="", *, task_limit=20):
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("工作资料查询最多 2000 字符")
        if chat_id and not any(
            c["chat_id"] == chat_id for c in self.runtime.hub.conversations()
        ):
            raise ValueError("会话不存在")
        projects = self.projects(chat_id)
        if project_id:
            selected = [p for p in projects if p["id"] == project_id]
            if not selected:
                raise ValueError("项目不存在或未授权给此会话")
        else:
            text = query.casefold()
            selected = [
                p
                for p in projects
                if any(
                    re.search(
                        r"(?<![A-Za-z0-9_])"
                        + re.escape(name.casefold())
                        + r"(?![A-Za-z0-9_])",
                        text,
                    )
                    if name.isascii()
                    else name.casefold() in text
                    for name in [p["name"], *p.get("aliases", [])]
                    if name
                )
            ]
            if not selected and len(projects) == 1:
                selected = projects
        selection_required = bool(
            query.strip() and projects and (not selected or len(selected) > 2)
        )
        if selection_required:
            selected = []
        evidence = []
        if query.strip():
            for project in selected[:2]:
                result = await asyncio.to_thread(search_project, project, query[:160])
                snippets = result["snippets"][:2]
                evidence.append(
                    {
                        **result,
                        "snippets": [{**s, "text": s["text"][:1600]} for s in snippets],
                        "limited": result["limited"]
                        or len(result["snippets"]) > 2
                        or any(len(s["text"]) > 1600 for s in snippets),
                    }
                )
        # Re-check permissions and edits after filesystem work yielded control.
        latest = {p["id"]: p for p in self.projects(chat_id)}
        valid = [p for p in projects if latest.get(p["id"]) == p]
        valid_ids = {p["id"] for p in valid}
        evidence = [e for e in evidence if e["project_id"] in valid_ids]
        changed = len(valid) != len(projects)
        selected_ids = {p["id"] for p in selected}
        catalog = [p for p in valid if not selected_ids or p["id"] in selected_ids]
        work = self.runtime.hub.work_tasks(limit=task_limit, chat_id=chat_id)
        return {
            "scope": "recipient" if chat_id else "owner",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "projects": project_catalog(
                catalog, profile_limit=4000 if project_id else 800
            ),
            "searches": evidence,
            "selection_required": selection_required,
            "configuration_changed": changed,
            "tasks": [
                {
                    k: (v[:400] if isinstance(v, str) else v)
                    for k, v in task.items()
                    if k
                    in {
                        "id",
                        "title",
                        "details",
                        "status",
                        "priority",
                        "chat_id",
                        "anchor",
                        "due_at",
                        "updated_at",
                    }
                }
                for task in work["items"]
            ],
            "task_total": work["total"],
            "tasks_truncated": len(work["items"]) < work["total"],
            "limitations": [
                "职责档案来自本人配置或历史工作聊天，不证明当前进度；截断字段不是完整档案。",
                "源码与文档是当前本地资料，不能证明已部署、已验收或替本人承诺排期。",
                "待办范围是本人或指定会话的未完成事项，不能仅凭会话关联断言属于某个项目。",
                "未命中或资料不可用不代表功能不存在；项目不明确时先选择项目。",
                "owner 范围仅供本人使用；对外回复必须用收件 chat_id 重新查询授权资料。",
            ],
        }

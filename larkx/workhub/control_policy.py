"""Bound console plans to operations explicitly requested by the owner.

This is a conservative command vocabulary, not a general language parser.
The planner must still resolve recipients, negation, conditions and ambiguity.
Quoted examples and message bodies must not authorize unrelated operations.
"""

import re


OPERATIONS = {
    "send_message": r"发送|发给|发到|回复|通知|转告|告诉(?!我)|联系|发(?:一条|一则|个消息|条消息|消息|一下|下|[\s：:]|$)|\b(?:send|reply|notify)\b",
    "create_task": r"(?:创建|新建|新增|添加|加|建|记|记录|记下|记个|记一条|加个|加一条).{0,12}(?:待办|任务)|(?:待办|任务).{0,8}(?:创建|新建|新增|添加)|\b(?:create|add)\b.{0,20}\b(?:task|todo)\b",
    "complete_task": r"(?:标记|设为|改为|置为).{0,10}(?:完成|done)|(?:勾掉|划掉|关闭|完成).{0,8}(?:待办|任务记录)|\b(?:mark|close)\b.{0,30}\b(?:done|complete|task|todo)\b",
    "update_profile": r"(?:修改|更新|设置|调整|改|清空|删除).{0,16}(?:角色|档案|背景|关系)|(?:角色|档案|背景|关系).{0,16}(?:改为|改成|设为|更新|清空|删除)|\b(?:update|edit|clear)\b.{0,20}\b(?:profile|role)\b",
}
READ_REQUEST = re.compile(
    r"^(?:请|帮我|麻烦|你|先|只|仅|一下|\s)*"
    r"(?:看看|查看|查询|搜索|列出|统计|总结|解释|说明|分析|检查|写|起草|润色|拟|如何|怎么|怎样)"
    r"|^(?:please\s+)?(?:show|list|find|summari[sz]e|explain|draft|write|polish)\b",
    re.I,
)


def allowed_control_actions(text):
    # Message payloads can contain e.g. “创建任务”; only the owner's command counts.
    instruction = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|`[^`]*`', "", text)
    commands = []
    for line in instruction.splitlines():
        head, *tail = re.split(r"[：:]", line, maxsplit=1)
        commands.append(
            head if tail and re.search(OPERATIONS["send_message"], head, re.I) else line
        )
    instruction = "\n".join(commands).strip()
    allowed = set()
    for kind, pattern in OPERATIONS.items():
        if not re.search(pattern, instruction, re.I):
            continue
        if re.search(
            r"(?:不要|别|禁止|勿|不需要|不用|不必|暂不)[^，,。;；\n]{0,8}(?:" + pattern + r")",
            instruction,
            re.I,
        ):
            continue
        if re.search(
            r"\b(?:do not|don't|never)\s+(?:" + pattern + r")", instruction, re.I
        ):
            continue
        if READ_REQUEST.search(instruction):
            # “总结进展” is read-only; “总结进展，然后发给小王” is an explicit action.
            if not re.search(
                r"(?:然后|接着|之后|后|并|再|\bthen\b|\band\b).{0,12}(?:"
                + pattern
                + r")",
                instruction,
                re.I,
            ):
                continue
            if re.search(
                r"(?:只|仅)(?:需|要)?(?:查询|查看|总结|起草|润色|写)", instruction
            ):
                continue
        allowed.add(kind)
    return allowed

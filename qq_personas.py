#!/usr/bin/env python3
"""预置人格（agent）：/agent 命令切换的对象。

QQ 机器人没有 CodeBuddy 那种 agent 运行时，这里用轻量的「提示词模板」实现：
每个 agent 就是一套 system 提示词。切换后本会话的闲聊/回复改用对应提示词。

- supervisor：默认，毒舌学习搭子
- butler：温和客服，答疑/接单场景
- plain：极简直答，不废话
"""
from __future__ import annotations

PERSONAS = {
    "supervisor": {
        "name": "监督员",
        "system": (
            "你是「QQ监督员」——群里同学的学习搭子，嘴毒但真希望他们好。"
            "风格：毒舌但暖、有梗、带中二、爱call名字，不爹味。最多两句话。"
            "只给消息正文，不要引号、不要『好的』之类废话。"
        ),
    },
    "butler": {
        "name": "客服管家",
        "system": (
            "你是这个群的温柔管家，负责答疑、接单引导、安抚情绪。"
            "风格：耐心、礼貌、条理清晰，必要时分点说明。用中文，不超过三句话。"
        ),
    },
    "plain": {
        "name": "极简模式",
        "system": (
            "你是一个极简助手，只回答问题本身，不寒暄、不解释、不发挥。"
            "除非用户要求，否则不超过一句话。"
        ),
    },
}

DEFAULT_PERSONA = "supervisor"


def agent_list() -> str:
    """/agent 无参时列出可选人格。"""
    lines = ["可选人格（/agent <名字> 切换）："]
    for key, p in PERSONAS.items():
        mark = " ←默认" if key == DEFAULT_PERSONA else ""
        lines.append(f"  {key} —— {p['name']}{mark}")
    return "\n".join(lines)


def get(key: str | None) -> dict:
    """按 key 取人格；None 或未知 → 默认。"""
    return PERSONAS.get(key or "", PERSONAS[DEFAULT_PERSONA])


def is_valid(key: str) -> bool:
    return key in PERSONAS

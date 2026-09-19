"""消息模型。

Agent 的历史其实就是一串消息。这里把它显式建模，
而不是到处传裸 dict——因为后面要做裁剪、折叠、摘要，
有一个能挂元数据的结构会省很多事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    # assistant 发起工具调用时携带
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # role=tool 时，指向被响应的 tool_call
    tool_call_id: str = ""
    name: str = ""
    # 内部元数据，不发给模型
    meta: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- 构造快捷方式
    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    # ------------------------------------------------------------------ 序列化
    def to_api(self) -> dict[str, Any]:
        """转成 OpenAI 格式。空字段必须删掉，否则部分服务会 400。"""
        body: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            body["tool_calls"] = self.tool_calls
            # 触发工具调用时 content 允许为空串，但字段要保留
            body.setdefault("content", "")
        if self.role == "tool":
            body["tool_call_id"] = self.tool_call_id
            if self.name:
                body["name"] = self.name
        return body

    @property
    def is_tool_output(self) -> bool:
        return self.role == "tool"

    def token_estimate(self) -> int:
        """粗略 token 估算，只用于裁剪决策。"""
        text = self.content or ""
        if self.tool_calls:
            text += json.dumps(self.tool_calls, ensure_ascii=False)
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk + max(1, (len(text) - cjk) // 4)


def build_tool_calls_payload(calls: list[Any]) -> list[dict[str, Any]]:
    """把 provider.ToolCallRequest 列表转成 API 需要的 tool_calls 结构。

    模型下一轮必须看到自己上一轮"报出的原文"，所以这里优先用 raw_arguments，
    保证历史里的 JSON 字符串和当时发出的完全一致。
    """
    out: list[dict[str, Any]] = []
    for call in calls:
        args = call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)
        out.append(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": args},
            }
        )
    return out

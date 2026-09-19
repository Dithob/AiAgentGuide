"""Provider 抽象：把"一次模型调用"归一化成可替换的组件。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(slots=True)
class ToolCallRequest:
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # 原始字符串在 JSON 解析失败时保留，便于把错误原样回灌给模型
    raw_arguments: str = ""


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(slots=True)
class ChatResponse:
    """一次模型调用的结果。"""

    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(ABC):
    """模型服务抽象。

    实现者只需要保证：给消息和工具 schema，返回文本 + 可选的工具调用请求。
    """

    name: str = "provider"

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        """调用一次模型。

        Args:
            on_delta: 若提供且实现支持流式，则每收到一段正文就回调一次。
                注意：**只有正文会回调，工具调用不会**——工具调用要等所有分片
                拼装完才有意义，半截的 JSON 没有任何用处。
                回调抛异常应当被视为"用户不想要这段输出了"，
                实现可以选择停止推送但不要中断整个请求。
        """

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """估算 token 数。用于上下文预算，不要求精确。"""

    def supports_streaming(self) -> bool:
        """默认不支持。上层可据此决定是否显示"逐字输出"。"""
        return False

    def close(self) -> None:  # pragma: no cover - 默认无资源需释放
        """释放底层连接。"""

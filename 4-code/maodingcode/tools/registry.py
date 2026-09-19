"""工具注册中心。

三件事：
  1. 登记工具，生成给模型的 schema 列表
  2. 分发调用（名字 -> 工具 -> 校验 -> 执行）
  3. 在批量注册时把可逆性回填给权限策略，保证两边不会各写一份表

关于"工具太多"：把所有工具 schema 一次性塞给模型，会挤占上下文并降低选对工具的概率。
当工具数超过阈值时，这里会自动退化成**索引模式**——只给模型一个
`list_tools` + `call_tool` 的入口，按需加载。这个开关不用改业务代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..errors import ToolNotFoundError
from ..security.policy import Policy, Reversibility, reversibility_of
from ..core.observability import Tracer
from .base import Tool

# 超过这个数量就切到索引模式
DIRECT_EXPOSURE_LIMIT = 40


@dataclass
class ToolRegistry:
    policy: Policy | None = None
    tracer: Tracer | None = None
    index_mode_threshold: int = DIRECT_EXPOSURE_LIMIT

    _tools: dict[str, Tool] = field(default_factory=dict)

    # ------------------------------------------------------------------ 注册
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool
        if self.policy is not None:
            self.policy.register_reversibility(tool.name, reversibility_of(tool))
        return tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.register(t)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._tools)) or "（无）"
            raise ToolNotFoundError(f"未注册的工具 {name!r}。可用：{available}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def by_source(self, prefix: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.source.startswith(prefix)]

    # ------------------------------------------------------------------ schema
    def schemas(self, allow: set[str] | None = None) -> list[dict[str, Any]]:
        """返回发给模型的工具 schema。

        索引模式下只返回两个元工具，其余靠 list_tools 按需取——
        这是控制上下文占用最有效的一招，因为 schema 是每轮都要重发的常量。
        """
        if allow is not None:
            return [self._tools[n].to_schema() for n in sorted(allow) if n in self._tools]

        if len(self._tools) > self.index_mode_threshold:
            return [META_LIST_TOOLS, META_CALL_TOOL]

        return [t.to_schema() for t in self.all()]

    def is_index_mode(self) -> bool:
        return len(self._tools) > self.index_mode_threshold

    # ------------------------------------------------------------------ 分发
    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        # 索引模式下模型调的是元工具，这里做一次转发
        if name == "list_tools":
            return self._describe_for_model(str(args.get("keyword") or ""))
        if name == "call_tool" and self.is_index_mode():
            real = str(args.get("tool") or "")
            inner = args.get("arguments") or {}
            if not isinstance(inner, dict):
                return "call_tool.arguments 必须是对象"
            return self.dispatch(real, inner)

        tool = self.get(name)
        err = tool.validate_args(args)
        if err:
            # 参数错误是"可自愈"的：如实回灌，模型通常下一轮就改对了
            return f"参数错误：{err}\n期望的 schema：{_schema_brief(tool)}"
        return tool.call(**args)

    def _describe_for_model(self, keyword: str) -> str:
        """给模型看的工具清单，含一行式参数摘要。"""
        lines = ["可用工具（name — 说明 — 主要参数）："]
        keyword = keyword.lower()
        for t in self.all():
            if keyword and keyword not in t.name.lower() and keyword not in t.description.lower():
                continue
            params = ", ".join((t.parameters or {}).get("properties", {}).keys())
            tag = "只读" if t.read_only else "写入"
            lines.append(f"- {t.name} [{tag}] — {t.description.splitlines()[0]} — 参数：{params or '无'}")
        if len(lines) == 1:
            lines.append("（没有匹配的工具）")
        return "\n".join(lines)

    def describe(self) -> str:
        rows = [f"{'工具':<18}{'类型':<8}{'来源':<20}说明"]
        rows.append("-" * 88)
        for t in self.all():
            kind = "只读" if t.read_only else "写入"
            rows.append(f"{t.name:<18}{kind:<8}{t.source:<20}{t.description.splitlines()[0][:36]}")
        return "\n".join(rows)


META_LIST_TOOLS = {
    "type": "function",
    "function": {
        "name": "list_tools",
        "description": "按关键词搜索可用工具。工具很多时先用它检索，再用 call_tool 调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "关键词，留空则列出全部工具",
                }
            },
            "required": [],
        },
    },
}

META_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "call_tool",
        "description": "调用通过 list_tools 查到的工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "工具名"},
                "arguments": {"type": "object", "description": "工具参数对象"},
            },
            "required": ["tool", "arguments"],
        },
    },
}


def _schema_brief(tool: Tool) -> str:
    props = (tool.parameters or {}).get("properties", {})
    required = set((tool.parameters or {}).get("required", []))
    parts = []
    for key, spec in props.items():
        mark = "*" if key in required else ""
        parts.append(f"{key}{mark}:{spec.get('type', 'any')}")
    return "{" + ", ".join(parts) + "}"


__all__ = ["ToolRegistry", "META_LIST_TOOLS", "META_CALL_TOOL", "DIRECT_EXPOSURE_LIMIT"]

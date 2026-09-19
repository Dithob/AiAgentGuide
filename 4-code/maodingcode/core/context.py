"""上下文工程。

一次模型调用真正发出去的东西 = 系统提示 + 历史消息 + 工具 schema。
上下文工程要解决的就是这三样东西的"装什么、装多少、怎么省"。

本模块负责三件事：
  1. 装配系统提示（分区块、按需注入，避免每轮都塞满）
  2. 控制历史长度（预算 + 折叠，优先牺牲早期工具输出）
  3. 记录被裁掉了什么（可观测性，也方便排查"模型怎么忘了"）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .message import Message
from ..errors import MaodingError

# 工具结果被折叠后替换成的占位符模板
FOLDED_TEMPLATE = "[历史工具输出已折叠：{tool}({args}) 返回 {size} 字符，摘要：{summary}]"


@dataclass(slots=True)
class ContextStats:
    """一次装配后的量化指标，直接喂给 Tracer。"""

    system_tokens: int = 0
    history_tokens: int = 0
    tool_schema_tokens: int = 0
    folded_messages: int = 0
    dropped_messages: int = 0

    @property
    def total(self) -> int:
        return self.system_tokens + self.history_tokens + self.tool_schema_tokens


@dataclass
class ContextManager:
    """维护会话历史并负责每次请求前的装配。"""

    workspace_root: Path
    max_context_tokens: int = 128_000
    # 给输出预留的比例，避免"输入塞满、输出没地方"
    output_reserve_ratio: float = 0.25
    keep_recent_turns: int = 6
    # 单个工具输出超过这个长度就先截断再入库
    max_tool_output_chars: int = 24_000

    messages: list[Message] = field(default_factory=list)
    _system_prompt: str = ""
    _base_sections: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ 系统提示装配
    def set_base_sections(self, sections: dict[str, str]) -> None:
        """设置基础区块：identity / environment / conventions 等。"""
        self._base_sections = {k: v for k, v in sections.items() if v}
        self._rebuild_system_prompt()

    def update_section(self, key: str, value: str) -> None:
        """动态更新某个区块，例如刷新可用技能索引。"""
        if value:
            self._base_sections[key] = value
        else:
            self._base_sections.pop(key, None)
        self._rebuild_system_prompt()

    def _rebuild_system_prompt(self) -> None:
        order = ("identity", "environment", "conventions", "memory", "skills", "extra")
        chunks: list[str] = []
        seen: set[str] = set()
        for key in order:
            if key in self._base_sections:
                chunks.append(self._base_sections[key])
                seen.add(key)
        for key, value in self._base_sections.items():
            if key not in seen:
                chunks.append(value)
        self._system_prompt = "\n\n".join(chunks)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    # ---------------------------------------------------------------- 历史操作
    def add(self, message: Message) -> None:
        if message.role == "tool":
            message.content = self._truncate_tool_output(message)
        self.messages.append(message)

    def extend(self, messages: Iterable[Message]) -> None:
        for m in messages:
            self.add(m)

    def clear(self) -> None:
        self.messages.clear()

    def _truncate_tool_output(self, message: Message) -> str:
        content = message.content or ""
        if len(content) <= self.max_tool_output_chars:
            return content
        head = content[: self.max_tool_output_chars // 2]
        tail = content[-self.max_tool_output_chars // 2 :]
        omitted = len(content) - len(head) - len(tail)
        return f"{head}\n\n... [省略 {omitted} 字符] ...\n\n{tail}"

    # ------------------------------------------------------------------ 装配
    def build(
        self,
        tool_schemas: list[dict[str, Any]] | None = None,
        counter=None,
    ) -> tuple[list[dict[str, Any]], ContextStats]:
        """产出最终发给模型的消息列表。

        Args:
            tool_schemas: 工具 schema 列表，仅用于估算占用。
            counter: 可调用对象 (str) -> int，默认用 4 字符/token 粗估。
        """
        count = counter or (lambda s: max(1, len(s) // 4))
        stats = ContextStats()
        tool_schemas = tool_schemas or []

        stats.system_tokens = count(self._system_prompt)
        stats.tool_schema_tokens = count(_schemas_to_text(tool_schemas))

        budget = int(self.max_context_tokens * (1 - self.output_reserve_ratio))
        budget -= stats.system_tokens + stats.tool_schema_tokens
        # 安全下限：预算再紧也要留出 2000 token 给系统提示 + 最近一轮，
        # 否则会把上下文裁到"模型连问题都看不见"的程度。
        # 注意：设了这个下限之后，把 context_window 配得很小并不会让裁剪更激进。
        budget = max(budget, 2_000)

        history = self._fit_history(budget, count, stats)

        payload: list[dict[str, Any]] = []
        if self._system_prompt:
            payload.append({"role": "system", "content": self._system_prompt})
        payload.extend(m.to_api() for m in history)
        return payload, stats

    def _fit_history(self, budget: int, count, stats: ContextStats) -> list[Message]:
        """在预算内保留尽可能完整的历史。

        策略分两级，成本从低到高：
          1) 折叠：把较早的工具输出压成一行摘要（保留结构，丢掉细节）
          2) 丢弃：按整轮丢掉最早的对话
        先折叠再丢弃，因为丢消息会破坏 tool_call / tool_result 的配对，
        而配对一旦破裂，多数服务会直接报错。
        """
        history = list(self.messages)
        total = sum(count(m.content or "") for m in history)

        recent_floor = self._recent_boundary(history)
        if total > budget:
            for idx in range(recent_floor):
                if total <= budget:
                    break
                msg = history[idx]
                if msg.role != "tool" or msg.meta.get("folded"):
                    continue
                before = count(msg.content or "")
                folded = FOLDED_TEMPLATE.format(
                    tool=msg.name or "tool",
                    args=_brief_args(msg),
                    size=len(msg.content or ""),
                    summary=_first_line(msg.content or ""),
                )
                msg.content = folded
                msg.meta["folded"] = True
                total -= before - count(folded)
                stats.folded_messages += 1

        # 仍然超预算 -> 整轮丢弃（从最早的成对消息开始）
        if total > budget:
            history, dropped, total = self._drop_rounds(history, budget, count, total)
            stats.dropped_messages = dropped

        stats.history_tokens = total
        return history

    def _recent_boundary(self, history: list[Message]) -> int:
        """最近 N 轮不允许折叠——模型需要完整上下文才能接着干活。"""
        turns = 0
        for i in range(len(history) - 1, -1, -1):
            if history[i].role == "user":
                turns += 1
                if turns > self.keep_recent_turns:
                    return i
        return 0

    @staticmethod
    def _drop_rounds(
        history: list[Message], budget: int, count, total: int
    ) -> tuple[list[Message], int, int]:
        """从最早一轮开始整轮丢弃，直到剩余部分装得进预算。

        这里踩过一个坑：早期版本只丢一轮就返回，结果在"严重超窗"的场景下
        （比如上面折叠完还是 1800 token，而预算只有 1100），丢一轮根本不够，
        请求照样会超窗。正确做法是**持续丢弃直到装得下**，同时保留最后一轮。

        另外必须整轮丢（user 开头到下一个 user 之前），不能只丢单条：
        tool_call 与 tool_result 一旦拆散，多数模型服务会直接返回 400。
        """
        counts = [count(m.content or "") for m in history]
        prefix = [0]
        for c in counts:
            prefix.append(prefix[-1] + c)
        total_all = prefix[-1]

        starts = [i for i, m in enumerate(history) if m.role == "user"]
        if len(starts) <= 1:
            # 只有一轮，无可丢；只能原样发出去（宁可超窗也要让当前任务继续）
            return history, 0, total_all

        for start in starts[1:]:
            remaining = total_all - prefix[start]
            if remaining <= budget:
                return history[start:], start, remaining

        last = starts[-1]
        return history[last:], last, total_all - prefix[last]


def _schemas_to_text(schemas: list[dict[str, Any]]) -> str:
    import json

    return json.dumps(schemas, ensure_ascii=False)


def _brief_args(message: Message) -> str:
    return str(message.meta.get("args_brief", ""))[:60]


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return "(空)"


def default_system_sections(workspace: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """生成默认的系统提示区块。

    刻意写得短。系统提示每轮都发，是最"贵"的常量——堆得越多，
    用户真正的问题能分到的注意力就越少。
    """
    sections = {
        "identity": (
            "你是 MaoDingCode，一个在用户本机运行的编码助手。\n"
            "你的目标是把用户的诉求落成仓库里真实的改动，而不是给建议。\n"
            "工作方式：先看代码再改代码；一次只做一件事；改完自己验证。"
        ),
        "environment": (
            f"工作区根目录：{workspace}\n"
            f"平台：{_platform()}\n"
            "所有相对路径都相对工作区根目录解析。"
        ),
        "conventions": (
            "约定：\n"
            "- 修改文件前先读取原内容，不确定就多读几个相关文件；\n"
            "- 优先做最小改动，不要顺手重构无关代码；\n"
            "- 工具报错时先读懂错误再重试，不要盲目重放同一调用；\n"
            "- 完成一段工作后用一两句话说明改了什么、为什么这么改。"
        ),
    }
    if extra:
        sections.update(extra)
    return sections


def _platform() -> str:
    import platform

    return f"{platform.system()} {platform.release()} / Python {platform.python_version()}"


__all__ = [
    "ContextManager",
    "ContextStats",
    "default_system_sections",
    "MaodingError",
]

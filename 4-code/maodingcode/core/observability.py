"""可观测性。

Agent 最容易被问住的问题是："你这次任务花了多少钱、慢在哪一步？"
没有埋点就只能靠猜。这里把三类指标收在一起：

  1. 步数 / 工具调用次数 —— 任务复杂度的代理指标
  2. token 用量（含缓存命中）—— 成本的主要来源
  3. 各阶段耗时 —— 定位"是模型慢还是工具慢"

不引第三方 tracing SDK，先把数据攒在内存里，需要时序列化成 JSON 落盘。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..provider.base import ChatResponse, Usage


@dataclass(slots=True)
class StepRecord:
    step: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    elapsed: float
    sent_messages: int
    tool_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolRecord:
    name: str
    ok: bool
    elapsed: float
    size: int


@dataclass(slots=True)
class Tracer:
    """一次会话的调用链记录。"""

    price_in_per_1k: float | None = None
    price_out_per_1k: float | None = None
    cache_discount: float = 0.1  # 缓存命中通常按 1 折计价，各厂商略有差异

    steps: list[StepRecord] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    started_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ 埋点
    def record_model_call(
        self, step: int, response: ChatResponse, elapsed: float, sent_messages: int
    ) -> None:
        u = response.usage
        self.usage = self.usage + u
        self.steps.append(
            StepRecord(
                step=step,
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
                cached_tokens=u.cached_tokens,
                elapsed=elapsed,
                sent_messages=sent_messages,
                tool_names=[c.name for c in response.tool_calls],
            )
        )

    def record_tool_call(self, name: str, ok: bool, elapsed: float, size: int) -> None:
        self.tools.append(ToolRecord(name=name, ok=ok, elapsed=elapsed, size=size))

    def reset(self) -> None:
        self.steps.clear()
        self.tools.clear()
        self.usage = Usage()
        self.started_at = time.time()

    # ------------------------------------------------------------------ 汇总
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def tool_call_count(self) -> int:
        return len(self.tools)

    @property
    def model_time(self) -> float:
        return sum(s.elapsed for s in self.steps)

    @property
    def tool_time(self) -> float:
        return sum(t.elapsed for t in self.tools)

    @property
    def cache_hit_rate(self) -> float:
        if not self.usage.prompt_tokens:
            return 0.0
        return self.usage.cached_tokens / self.usage.prompt_tokens

    def cost(self) -> float | None:
        """估算成本（单位与单价一致，通常是美元或人民币）。无单价时返回 None。"""
        if self.price_in_per_1k is None or self.price_out_per_1k is None:
            return None
        cached = self.usage.cached_tokens
        fresh_input = max(self.usage.prompt_tokens - cached, 0)
        in_cost = (
            fresh_input * self.price_in_per_1k
            + cached * self.price_in_per_1k * self.cache_discount
        ) / 1000
        out_cost = self.usage.completion_tokens * self.price_out_per_1k / 1000
        return in_cost + out_cost

    def summary(self) -> dict[str, Any]:
        cost = self.cost()
        return {
            "steps": self.step_count,
            "tool_calls": self.tool_call_count,
            "failed_tools": sum(1 for t in self.tools if not t.ok),
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "cached_tokens": self.usage.cached_tokens,
            "total_tokens": self.usage.total_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 3),
            "model_seconds": round(self.model_time, 2),
            "tool_seconds": round(self.tool_time, 2),
            "wall_seconds": round(time.time() - self.started_at, 2),
            "cost": None if cost is None else round(cost, 6),
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary(),
                "steps": [s.__dict__ for s in self.steps],
                "tools": [t.__dict__ for t in self.tools],
            },
            ensure_ascii=False,
            indent=2,
        )

    def dump(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p


__all__ = ["Tracer", "StepRecord", "ToolRecord"]

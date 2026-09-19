"""Agent Loop —— 整个项目的心脏。

一句话：**持续"问模型 → 执行工具 → 把结果塞回去"，直到模型不再要求工具调用。**

     while step < max_steps:
         resp = provider.chat(ctx.build(...))
         if not resp.wants_tools:
             return resp.content            # 收敛
         for call in resp.tool_calls:
             decision = policy.check(call)  # 权限闸门
             result = registry.dispatch(call)
             ctx.add(tool_message(result))
     return "达到步数上限"

工程上真正难的不是这个 while，而是四个"退出条件"：
  1. 模型不再调用工具 -> 正常收敛
  2. 达到 max_steps     -> 防止无限循环烧钱
  3. 用户中断           -> Ctrl+C 要能优雅退出并保留已完成的工作
  4. 连续失败           -> 同一个工具连续报错就别再重试了，如实上报

本实现把 1/2/4 做进循环，3 通过回调暴露给 UI。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import MaodingError, PermissionDenied, ToolNotFoundError
from ..provider.base import ChatResponse, Provider, ToolCallRequest
from ..security.policy import Decision, Policy, Verdict
from .context import ContextManager
from .message import Message, build_tool_calls_payload
from .observability import Tracer

# 同一个工具连续失败多少次要熔断
FAILURE_STREAK_LIMIT = 3


class StopReason(str, Enum):
    COMPLETED = "completed"        # 模型给出最终回答
    MAX_STEPS = "max_steps"        # 步数用尽
    ABORTED = "aborted"            # 用户中断
    FAILURE_LOOP = "failure_loop"  # 连续失败熔断
    ERROR = "error"


@dataclass(slots=True)
class LoopEvent:
    """循环过程中的事件。UI 层订阅它来做渲染，Loop 自己不打印任何东西。

    这是让"引擎"和"界面"解耦的关键：同一个 Loop 既能配终端 UI，
    也能配 Web UI 或测试用的静默收集器。
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[LoopEvent], None]
# 权限确认回调：(工具名, 参数, 拦截原因) -> 是否放行
ConfirmFn = Callable[[str, dict, str], bool]


@dataclass(slots=True)
class LoopResult:
    content: str
    reason: StopReason
    steps: int = 0
    tool_calls_made: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is StopReason.COMPLETED


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        context: ContextManager,
        registry,                  # tools.registry.ToolRegistry
        policy: Policy,
        tracer: Tracer | None = None,
        max_steps: int = 25,
        emit: EventSink | None = None,
        stream: bool = True,
        confirm: "ConfirmFn | None" = None,
    ) -> None:
        self.provider = provider
        self.context = context
        self.registry = registry
        self.policy = policy
        self.tracer = tracer or Tracer()
        self.max_steps = max_steps
        self._emit = emit or (lambda _e: None)
        self._abort = False
        # 只有"配置开了流式"且"Provider 支持"才逐字推送。
        # 否则模型一段话会等到全部生成完才出现，UI 上要空等十几秒。
        self.stream = stream and provider.supports_streaming()
        # 权限确认由外部注入。注入点放在构造参数上而不是事后改私有属性，
        # 是因为 CLI / RPC / 测试三种宿主都需要它，字段化更不容易漏。
        self._confirm_fn = confirm

    # ------------------------------------------------------------------ 控制
    def abort(self) -> None:
        """供 UI 在用户按 Ctrl+C 时调用。下一轮开始时生效。"""
        self._abort = True

    def _fire(self, kind: str, **payload: Any) -> None:
        self._emit(LoopEvent(kind=kind, payload=payload))

    # ------------------------------------------------------------------ 主循环
    def run(self, user_input: str) -> LoopResult:
        self._abort = False
        self.context.add(Message.user(user_input))
        self._fire("user_message", text=user_input)

        failure_streak = 0
        last_failure_key = ""
        tool_calls_made = 0

        for step in range(1, self.max_steps + 1):
            if self._abort:
                self._fire("stopped", reason=StopReason.ABORTED.value)
                return LoopResult("已中断。", StopReason.ABORTED, step - 1, tool_calls_made)

            schemas = self.registry.schemas() if self.registry else []
            payload, stats = self.context.build(
                tool_schemas=schemas, counter=self.provider.count_tokens
            )
            self._fire("step_start", step=step, stats=stats)

            response = self._call_provider(payload, schemas, step)
            if response is None:  # 已 fire error 事件
                return LoopResult("模型调用失败。", StopReason.ERROR, step, tool_calls_made)

            self.context.add(
                Message.assistant(
                    content=response.content,
                    tool_calls=build_tool_calls_payload(response.tool_calls),
                )
            )

            # --- 收敛：没有工具调用 -> 这就是最终答案 ---
            if not response.wants_tools:
                self._fire("assistant_message", text=response.content)
                self._fire("stopped", reason=StopReason.COMPLETED.value)
                return LoopResult(response.content, StopReason.COMPLETED, step, tool_calls_made)

            # --- 执行工具 ---
            for call in response.tool_calls:
                tool_calls_made += 1
                key = f"{call.name}:{sorted(call.arguments)}"
                if key == last_failure_key:
                    failure_streak += 1
                else:
                    failure_streak = 0
                    last_failure_key = key

                if failure_streak >= FAILURE_STREAK_LIMIT:
                    note = (
                        f"同一个工具调用已连续失败 {failure_streak} 次，"
                        f"停止重试并上报，请人工检查：{call.name}"
                    )
                    self.context.add(Message.tool_result(call.id, call.name, note))
                    self._fire("stopped", reason=StopReason.FAILURE_LOOP.value)
                    return LoopResult(note, StopReason.FAILURE_LOOP, step, tool_calls_made)

                self._execute_one(call)

            self._fire("step_end", step=step)

        note = f"已达到最大步数 {self.max_steps}，任务未收敛。已完成的改动保留在工作区。"
        self._fire("stopped", reason=StopReason.MAX_STEPS.value)
        return LoopResult(note, StopReason.MAX_STEPS, self.max_steps, tool_calls_made)

    # ------------------------------------------------------------------ 内部
    def _call_provider(
        self, payload: list[dict[str, Any]], schemas: list[dict[str, Any]], step: int
    ) -> ChatResponse | None:
        import time

        def on_delta(text: str) -> None:
            self._fire("delta", text=text)

        started = time.time()
        try:
            response = self.provider.chat(
                payload, schemas or None, on_delta=on_delta if self.stream else None
            )
        except MaodingError as exc:
            self._fire("error", stage="provider", message=str(exc))
            return None
        except KeyboardInterrupt:
            self.abort()
            self._fire("stopped", reason=StopReason.ABORTED.value)
            return None

        elapsed = time.time() - started
        self.tracer.record_model_call(
            step=step, response=response, elapsed=elapsed, sent_messages=len(payload)
        )
        self._fire(
            "model_response",
            step=step,
            content=response.content,
            tool_calls=[c.name for c in response.tool_calls],
            usage=response.usage,
            elapsed=elapsed,
        )
        return response

    def _execute_one(self, call: ToolCallRequest) -> None:
        import time

        # 1) 参数解析失败：把错误原文回灌，让模型自己修 JSON
        if call.raw_arguments and not call.arguments:
            message = (
                f"参数不是合法 JSON，请重新发起调用并输出合法 JSON。"
                f"收到的原文：{call.raw_arguments[:300]}"
            )
            self.context.add(Message.tool_result(call.id, call.name, message))
            self._fire("tool_result", name=call.name, ok=False, output=message)
            return

        # 2) 权限闸门
        try:
            decision: Decision = self.policy.check(call.name, call.arguments)
        except PermissionDenied as exc:
            text = f"权限拒绝：{exc}（原因：{exc.reason or '策略限制'}）"
            self.context.add(Message.tool_result(call.id, call.name, text))
            self._fire("tool_result", name=call.name, ok=False, output=text)
            return

        if decision.verdict is Verdict.DENY:
            text = f"权限拒绝：{decision.reason}"
            self.context.add(Message.tool_result(call.id, call.name, text))
            self._fire("tool_result", name=call.name, ok=False, output=text)
            return

        if decision.verdict is Verdict.ASK:
            approved = self._confirm(call, decision)
            if not approved:
                text = "用户拒绝了这次工具调用。请换一种方式，或询问用户想怎么做。"
                self.context.add(Message.tool_result(call.id, call.name, text))
                self._fire("tool_result", name=call.name, ok=False, output=text)
                return

        # 3) 真正执行
        self._fire("tool_start", name=call.name, args=call.arguments)
        started = time.time()
        try:
            output = self.registry.dispatch(call.name, call.arguments)
            ok = True
        except ToolNotFoundError as exc:
            output, ok = f"工具不存在：{exc}", False
        except Exception as exc:  # 工具异常不能掀翻整个循环
            output, ok = f"{type(exc).__name__}: {exc}", False
        elapsed = time.time() - started

        result_message = Message.tool_result(call.id, call.name, output)
        result_message.meta["args_brief"] = _brief(call.arguments)
        self.context.add(result_message)
        self.tracer.record_tool_call(call.name, ok=ok, elapsed=elapsed, size=len(output))
        self._fire("tool_result", name=call.name, ok=ok, output=output, elapsed=elapsed)

    def _confirm(self, call: ToolCallRequest, decision: Decision) -> bool:
        """把确认动作交给宿主（CLI 的 stdin / RPC 的前端 / 编辑器弹窗）。

        宿主没有注入或注入的实现抛异常时，一律按**拒绝**处理。
        默认拒绝是刻意的：权限系统出问题时要倒向"不做"，
        而不是悄悄放行一个 rm -rf。
        """
        if self._confirm_fn is None:
            return False
        try:
            return bool(self._confirm_fn(call.name, call.arguments, decision.reason))
        except Exception:
            return False


def _brief(args: dict[str, Any]) -> str:
    parts = []
    for k, v in list(args.items())[:3]:
        sv = str(v).replace("\n", " ")
        parts.append(f"{k}={sv[:40]}")
    return ", ".join(parts)


def run_once(
    user_input: str,
    *,
    provider: Provider,
    context: ContextManager,
    registry,
    policy: Policy,
    max_steps: int = 25,
    sink: EventSink | None = None,
) -> LoopResult:
    """一次性执行，便于测试与脚本化调用。"""
    loop = AgentLoop(
        provider=provider,
        context=context,
        registry=registry,
        policy=policy,
        max_steps=max_steps,
        emit=sink,
    )
    return loop.run(user_input)


__all__ = [
    "AgentLoop",
    "LoopEvent",
    "LoopResult",
    "StopReason",
    "run_once",
    "EventSink",
]

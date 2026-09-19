"""终端渲染。

只依赖 ANSI 转义序列，不引 rich/textual。理由很实际：
一个要在别人机器上跑起来的项目，少一个依赖就少一类"在我这跑不了"。

渲染原则：
  - 模型的正式回答用原样输出，不要加边框干扰阅读
  - 工具执行过程用缩进 + 灰色，让"我在干活"和"我在说话"视觉可分
  - 危险动作（写文件、执行命令）必须显式可见，不能静默
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import Any

# ------------------------------------------------------------------ 颜色开关
_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return text if _NO_COLOR else f"\033[{code}m{text}\033[0m"


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def red(t: str) -> str:
    return _c("31", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def blue(t: str) -> str:
    return _c("34", t)


def magenta(t: str) -> str:
    return _c("35", t)


def cyan(t: str) -> str:
    return _c("36", t)


# ------------------------------------------------------------------ 结构
def width(default: int = 88) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def banner(version: str, model: str, workspace: str, mode: str) -> str:
    line = "─" * min(width(), 72)
    return "\n".join(
        [
            bold(cyan("MaoDingCode")) + dim(f"  v{version}"),
            dim(line),
            f"  模型    {model}",
            f"  工作区  {workspace}",
            f"  权限    {_mode_label(mode)}",
            dim("  输入 /help 查看命令，Ctrl+C 中断当前任务，/quit 退出"),
            dim(line),
        ]
    )


def _mode_label(mode: str) -> str:
    return {
        "auto": green("auto（写操作不询问）"),
        "suggest": yellow("suggest（写操作需确认）"),
        "readonly": blue("readonly（只读）"),
    }.get(mode, mode)


def user_prompt() -> str:
    return bold(blue("› "))


def tool_line(name: str, args: dict[str, Any]) -> str:
    return dim("  ⚙ ") + dim(_fmt_args(name, args))


def _fmt_args(name: str, args: dict[str, Any]) -> str:
    if not args:
        return name
    parts = []
    for key, value in list(args.items())[:3]:
        sv = str(value).replace("\n", "⏎")
        if len(sv) > 56:
            sv = sv[:56] + "…"
        parts.append(f"{key}={sv}")
    suffix = "" if len(args) <= 3 else f" …(+{len(args) - 3})"
    return f"{name}({', '.join(parts)}{suffix})"


def tool_ok(name: str, elapsed: float) -> str:
    return dim(f"  ✓ {name} ") + dim(f"{elapsed:.2f}s")


def tool_fail(name: str, output: str) -> str:
    head = red(f"  ✗ {name} 失败")
    body = textwrap.indent(_clip(output, 600), "    ")
    return f"{head}\n{dim(body)}"


def tool_output_preview(output: str, limit: int = 12) -> str:
    lines = output.splitlines()
    shown = lines[:limit]
    text = textwrap.indent("\n".join(shown), "    ")
    if len(lines) > limit:
        text += dim(f"\n    …（还有 {len(lines) - limit} 行）")
    return dim(text)


def assistant(text: str) -> str:
    return f"\n{text}\n"


def error(text: str) -> str:
    return red(f"✗ {text}")


def warn(text: str) -> str:
    return yellow(f"! {text}")


def hint(text: str) -> str:
    return dim(f"  {text}")


def confirm_prompt(tool_name: str, args: dict[str, Any], reason: str) -> str:
    """权限确认框。

    要让人能在 1 秒内判断该不该点 y，所以：命令原文必须完整可见、
    要改的文件必须带路径、必须写明为什么被拦下来。
    """
    w = min(width(), 76)
    lines = [
        "",
        yellow("┌" + "─" * (w - 2) + "┐"),
        yellow("│ ") + bold(f"需要确认：{tool_name}") + " " * max(0, w - 4 - len(tool_name) - 5) + yellow("│"),
        yellow("├" + "─" * (w - 2) + "┤"),
    ]
    if reason:
        for seg in textwrap.wrap(reason, w - 6) or [""]:
            lines.append(yellow("│ ") + seg.ljust(w - 4) + yellow(" │"))
    for key, value in args.items():
        body = f"{key}: {value}"
        for i, seg in enumerate(textwrap.wrap(body.replace("\n", "⏎"), w - 6) or [""]):
            prefix = "  " if i == 0 else "    "
            lines.append(yellow("│ ") + (prefix + seg).ljust(w - 4) + yellow(" │"))
    lines.append(yellow("└" + "─" * (w - 2) + "┘"))
    lines.append(bold("  执行？[y] 本次允许  [a] 全部允许  [n] 拒绝  → ") + "")
    return "\n".join(lines)


def cost_report(summary: dict[str, Any]) -> str:
    cost = summary.get("cost")
    cost_text = dim("未配置单价") if cost is None else bold(f"{cost:.6f}")
    rows = [
        ("步数", f"{summary['steps']}"),
        ("工具调用", f"{summary['tool_calls']}（失败 {summary['failed_tools']}）"),
        (
            "token",
            f"输入 {summary['prompt_tokens']} / 输出 {summary['completion_tokens']}"
            f" / 命中缓存 {summary['cached_tokens']}"
            f"（命中率 {summary['cache_hit_rate']:.1%}）",
        ),
        ("耗时", f"模型 {summary['model_seconds']}s / 工具 {summary['tool_seconds']}s"),
        ("预估成本", cost_text),
    ]
    body = "\n".join(f"  {k:<10}{v}" for k, v in rows)
    return dim("─" * 40) + "\n" + body


def render_event(event_kind: str, payload: dict[str, Any]) -> str | None:
    """把 Loop 事件映射成可打印文本。返回 None 表示该事件不渲染。

    这是 UI 与引擎之间唯一的耦合点——想换界面（Web/TUI）就换这一个函数。
    """
    if event_kind == "tool_start":
        return tool_line(payload["name"], payload.get("args") or {})
    if event_kind == "tool_result":
        if payload.get("ok"):
            return tool_ok(payload["name"], payload.get("elapsed", 0.0))
        return tool_fail(payload["name"], str(payload.get("output", "")))
    if event_kind == "error":
        return error(f"[{payload.get('stage', 'runtime')}] {payload.get('message', '')}")
    if event_kind == "stopped":
        reason = payload.get("reason")
        if reason and reason != "completed":
            return warn(f"循环结束：{reason}")
    return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "banner", "user_prompt", "tool_line", "tool_ok", "tool_fail",
    "tool_output_preview", "assistant", "error", "warn", "hint",
    "confirm_prompt", "cost_report", "render_event", "width",
    "bold", "dim", "red", "green", "yellow", "blue", "magenta", "cyan",
]

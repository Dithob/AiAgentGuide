"""SSE 流式响应解析。

流式比一次性返回复杂在**同一个逻辑响应会被切成很多片**，而工具调用的
参数是分成多个分片拼出来的：

    chunk 1: delta.tool_calls[0] = {index:0, id:"call_1", function:{name:"write_file", arguments:""}}
    chunk 2: delta.tool_calls[0] = {index:0, function:{arguments:"{\\"path\\":"}}
    chunk 3: delta.tool_calls[0] = {index:0, function:{arguments:"\\"a.txt\\"}"}}
    chunk 4: delta.content = ""; finish_reason = "tool_calls"
    data: [DONE]

所以必须按 `index` 累积，不能覆盖。这里把累积逻辑抽成纯函数，
不碰网络，方便单测——分片边界是最容易出 bug 的地方。

本模块不依赖任何 HTTP 库：喂进来的只是一个可迭代的字节行序列。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .base import ChatResponse, ToolCallRequest, Usage

SSE_DATA_PREFIX = "data:"
SSE_DONE = "[DONE]"


def iter_sse_json(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    """把 SSE 行流转成一个个 JSON 对象。

    只做三件事：跳过空行与注释行、剥掉 `data:` 前缀、遇到 `[DONE]` 停止。
    解析失败的行直接跳过——有些服务会在流里插入心跳或日志。
    """
    for raw in lines:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace")
        else:
            line = raw
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith(SSE_DATA_PREFIX):
            payload = line[len(SSE_DATA_PREFIX) :].strip()
        elif line.startswith("{"):
            # 少数服务直接吐 JSON 行，不带 data: 前缀
            payload = line
        else:
            continue
        if payload == SSE_DONE:
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def merge_tool_name(stored: str, incoming: str) -> str:
    """合并可能被拆分的工具名。

    OpenAI 只在第一个分片给完整 name，但部分兼容实现会拆开发
    （"read" + "_file"）。用"后缀已包含就不重复追加"的方式兼容两种情况：

        stored="",      incoming="read_file"  -> "read_file"
        stored="read_file", incoming="read_file" -> "read_file"   （重复整名，忽略）
        stored="read",  incoming="_file"      -> "read_file"      （分片拼接）
    """
    if not incoming:
        return stored
    if not stored:
        return incoming
    if stored.endswith(incoming):
        return stored
    return stored + incoming


@dataclass
class StreamAccumulator:
    """把一串分片还原成一个完整的 ChatResponse。"""

    content_parts: list[str] = field(default_factory=list)
    _tool_slots: dict[int, dict[str, Any]] = field(default_factory=dict)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    saw_any_chunk: bool = False

    # ------------------------------------------------------------------ 喂数据
    def feed(self, chunk: dict[str, Any]) -> str:
        """吃下一个分片，返回本次新增的正文（没有则空串）。"""
        self.saw_any_chunk = True

        # usage 通常只在最后一个分片里出现（需请求时带 stream_options.include_usage）
        if isinstance(chunk.get("usage"), dict):
            self.usage = parse_usage(chunk["usage"])

        delta_text = ""
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])

            # delta 是流式的；message 是非流式格式，一并兼容（有些服务混着发）
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue

            text = delta.get("content")
            if isinstance(text, list):  # 分段内容
                text = "".join(
                    p.get("text", "") for p in text if isinstance(p, dict)
                )
            if text:
                self.content_parts.append(str(text))
                delta_text += str(text)

            self._feed_tool_calls(delta.get("tool_calls"))

        return delta_text

    def _feed_tool_calls(self, calls: Any) -> None:
        if not isinstance(calls, list):
            return
        for pos, item in enumerate(calls):
            if not isinstance(item, dict):
                continue
            # 优先用服务给的 index；没给就用数组下标兜底
            idx = item.get("index")
            idx = int(idx) if isinstance(idx, int) else pos

            slot = self._tool_slots.setdefault(
                idx, {"id": "", "name": "", "args": ""}
            )
            if item.get("id"):
                slot["id"] = str(item["id"])

            fn = item.get("function")
            if isinstance(fn, dict):
                slot["name"] = merge_tool_name(slot["name"], str(fn.get("name") or ""))
                args = fn.get("arguments")
                if isinstance(args, str):
                    slot["args"] += args
                elif args is not None:
                    slot["args"] += json.dumps(args, ensure_ascii=False)

    # ------------------------------------------------------------------ 结果
    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    def result(self) -> ChatResponse:
        calls: list[ToolCallRequest] = []
        for idx in sorted(self._tool_slots):
            slot = self._tool_slots[idx]
            if not slot["name"]:
                continue
            raw = slot["args"] or "{}"
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    parsed = {"_value": parsed}
                ok = True
            except json.JSONDecodeError:
                parsed, ok = {}, False
            calls.append(
                ToolCallRequest(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments=parsed,
                    raw_arguments="" if ok else raw,
                )
            )

        return ChatResponse(
            content=self.content,
            tool_calls=calls,
            usage=self.usage,
            finish_reason=self.finish_reason,
            raw={"streamed": True, "chunks_ok": self.saw_any_chunk},
        )


def parse_usage(raw: dict[str, Any]) -> Usage:
    """兼容多家字段名。DeepSeek 用 prompt_cache_hit_tokens，
    OpenAI 用 prompt_tokens_details.cached_tokens。"""
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    cached = int(
        raw.get("prompt_cache_hit_tokens")
        or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        total_tokens=int(raw.get("total_tokens") or (prompt + completion)),
    )


__all__ = [
    "StreamAccumulator",
    "iter_sse_json",
    "merge_tool_name",
    "parse_usage",
    "SSE_DONE",
]

"""流式解析与 Provider 流式的测试。

跑法（在 4-code/ 目录下）：python maodingcode/tests/test_streaming.py
     （也可被 pytest 收集）

重点是**分片边界**：工具参数被切成 5 个字符一片时，
能不能原样拼回来。这是流式实现最容易出错的地方。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# IMPORT_ROOT = maodingcode 包的父目录（即 4-code/）。
# 项目根与包目录已合并成同一层，所以这里必须上溯两层（parents[2]）而不是 parent.parent。
IMPORT_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for _p in (str(IMPORT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from maodingcode.config import ProviderConfig  # noqa: E402
from maodingcode.errors import ProviderError  # noqa: E402
from maodingcode.provider.openai_compat import OpenAICompatProvider  # noqa: E402
from maodingcode.provider.streaming import (  # noqa: E402
    StreamAccumulator,
    iter_sse_json,
    merge_tool_name,
)
from fake_llm import (  # noqa: E402
    FakeLLM,
    http_error_response,
    text_response,
    to_sse_chunks,
    tool_call_response,
)


def _pass(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise AssertionError(msg)


# --------------------------------------------------------------------- SSE 行解析
def test_iter_sse_json() -> None:
    print("test_iter_sse_json")
    lines = [
        ": 这是注释行，应被跳过\n".encode("utf-8"),
        b"\n",
        b'data: {"a": 1}\n',
        b'data: {"b": 2}\n',
        b"data: [DONE]\n",
        b'data: {"c": 3}\n',  # [DONE] 之后的不应再被读到
    ]
    got = [o for o in iter_sse_json(lines)]
    if got != [{"a": 1}, {"b": 2}]:
        _fail(f"SSE 行解析错误：{got}")
    _pass("剥前缀 / 跳注释 / [DONE] 终止 都正确")

    # 不带 data: 前缀的裸 JSON 行也应兼容
    got = [o for o in iter_sse_json(['{"x": 9}\n', "垃圾行\n"])]
    if got != [{"x": 9}]:
        _fail(f"裸 JSON 行兼容失败：{got}")
    _pass("兼容不带 data: 前缀的服务")

    # 解析失败的行不应让整个流崩掉
    got = [o for o in iter_sse_json(['data: {坏json\n', 'data: {"ok": 1}\n'])]
    if got != [{"ok": 1}]:
        _fail(f"坏行应被跳过：{got}")
    _pass("坏行被跳过，不中断流")


def test_merge_tool_name() -> None:
    print("test_merge_tool_name")
    cases = [
        ("", "read_file", "read_file"),
        ("read_file", "read_file", "read_file"),   # 重复整名，不能拼成两个
        ("read", "_file", "read_file"),            # 分片拼接
        ("read_file", "", "read_file"),            # 空的不影响
    ]
    for stored, incoming, expected in cases:
        got = merge_tool_name(stored, incoming)
        if got != expected:
            _fail(f"merge({stored!r}, {incoming!r}) = {got!r}，期望 {expected!r}")
    _pass("工具名合并（含重复整名与分片两种情况）正确")


# --------------------------------------------------------------------- 累加器
def test_accumulator_text() -> None:
    print("test_accumulator_text")
    acc = StreamAccumulator()
    deltas = []
    for chunk in [
        {"choices": [{"delta": {"content": "你好"}}]},
        {"choices": [{"delta": {"content": "，世界"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]:
        deltas.append(acc.feed(chunk))
    if "".join(deltas) != "你好，世界":
        _fail(f"增量正文拼接错误：{deltas}")
    res = acc.result()
    if res.content != "你好，世界" or res.finish_reason != "stop":
        _fail(f"结果错误：{res}")
    if res.usage.total_tokens != 15:
        _fail(f"usage 未解析：{res.usage}")
    _pass(f"文本流累积正确，usage 解析正确（{res.usage.total_tokens} tokens）")


def test_accumulator_tool_call_fragmented() -> None:
    print("test_accumulator_tool_call_fragmented")
    response = tool_call_response(
        "write_file",
        {"path": "src/很长的路径/文件.py", "content": "print('中文内容')\n" * 5},
    )
    acc = StreamAccumulator()
    for chunk in to_sse_chunks(response, arg_fragment=5):  # 每片仅 5 字符
        acc.feed(chunk)
    res = acc.result()

    if not res.wants_tools:
        _fail("跨分片的工具调用没有装配出来")
    call = res.tool_calls[0]
    if call.name != "write_file":
        _fail(f"工具名错误：{call.name!r}")
    if call.arguments.get("path") != "src/很长的路径/文件.py":
        _fail(f"参数 path 拼装错误：{call.arguments.get('path')!r}")
    if call.arguments.get("content") != "print('中文内容')\n" * 5:
        _fail("参数 content 拼装错误（多字节字符被截断？）")
    if call.raw_arguments:
        _fail("参数是可解析的，raw_arguments 应为空")
    _pass("被切成 5 字符一片的工具参数完整拼回（含中文与换行）")


def test_accumulator_parse_failure_is_preserved() -> None:
    print("test_accumulator_parse_failure_is_preserved")
    acc = StreamAccumulator()
    # 模型偶尔会输出语法坏掉的 JSON
    for frag in ['{"path":', '"a.txt"', ", }"]:
        acc.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": frag}}]}}]})
    res = acc.result()
    if res.tool_calls[0].arguments:
        _fail("坏 JSON 不应被解析出参数")
    if not res.tool_calls[0].raw_arguments:
        _fail("坏 JSON 的原文必须保留，否则模型无法自愈")
    _pass("坏 JSON 参数保留原文，交给 Loop 回灌")


def test_accumulator_multiple_tool_calls() -> None:
    print("test_accumulator_multiple_tool_calls")
    acc = StreamAccumulator()
    # 并行工具调用：两个 index 交错到达
    seq = [
        {"index": 0, "id": "c0", "function": {"name": "read_file", "arguments": '{"path":'}},
        {"index": 1, "id": "c1", "function": {"name": "list_dir", "arguments": '{"path":'}},
        {"index": 0, "function": {"arguments": '"a.py"}'}},
        {"index": 1, "function": {"arguments": '"."}'}},
    ]
    for item in seq:
        acc.feed({"choices": [{"delta": {"tool_calls": [item]}}]})
    res = acc.result()
    names = [c.name for c in res.tool_calls]
    if names != ["read_file", "list_dir"]:
        _fail(f"并行工具调用装配错误：{names}")
    if res.tool_calls[0].arguments != {"path": "a.py"}:
        _fail(f"index=0 参数错误：{res.tool_calls[0].arguments}")
    if res.tool_calls[1].arguments != {"path": "."}:
        _fail(f"index=1 参数错误：{res.tool_calls[1].arguments}")
    _pass("交错到达的多个工具调用按 index 正确分流")


# --------------------------------------------------------------------- Provider
def _provider(base_url: str, stream: bool = True) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        ProviderConfig(
            api_key="test-key",
            base_url=base_url,
            model="fake-model",
            timeout=10.0,
            stream=stream,
        )
    )


def test_provider_streaming_end_to_end() -> None:
    print("test_provider_streaming_end_to_end")
    with FakeLLM([text_response("流式输出测试内容")]) as llm:
        provider = _provider(llm.base_url)
        if not provider.supports_streaming():
            _fail("配置了 stream 应报告支持流式")
        chunks: list[str] = []
        resp = provider.chat([{"role": "user", "content": "hi"}], on_delta=chunks.append)

        if resp.content != "流式输出测试内容":
            _fail(f"最终正文错误：{resp.content!r}")
        if "".join(chunks) != "流式输出测试内容":
            _fail(f"增量拼接错误：{chunks}")
        if len(chunks) < 2:
            _fail(f"应当收到多个分片，实际 {len(chunks)} 个")
        if resp.usage.total_tokens != 120:
            _fail(f"usage 未从流中解析：{resp.usage}")

        # 请求里必须带 stream: true，否则服务端不会走 SSE
        sent = llm.last_request()["body"]
        if not sent.get("stream"):
            _fail("请求未声明 stream")
        _pass(f"流式请求成功，收到 {len(chunks)} 个分片，usage 正确")


def test_provider_streaming_tool_call() -> None:
    print("test_provider_streaming_tool_call")
    with FakeLLM([tool_call_response("write_file", {"path": "out/demo.txt", "content": "中文内容"})]) as llm:
        provider = _provider(llm.base_url)
        resp = provider.chat([{"role": "user", "content": "写文件"}], on_delta=lambda _t: None)
        if not resp.wants_tools:
            _fail("流式下工具调用丢失")
        if resp.tool_calls[0].arguments.get("content") != "中文内容":
            _fail(f"参数错误：{resp.tool_calls[0].arguments}")
        _pass("流式下的工具调用跨分片装配正确")


def test_provider_no_stream_when_no_consumer() -> None:
    print("test_provider_no_stream_when_no_consumer")
    with FakeLLM([text_response("普通响应")]) as llm:
        provider = _provider(llm.base_url)
        resp = provider.chat([{"role": "user", "content": "hi"}])  # 不给 on_delta
        if resp.content != "普通响应":
            _fail(f"一次性响应错误：{resp.content!r}")
        if llm.last_request()["body"].get("stream"):
            _fail("没有消费方时不应请求流式")
        _pass("无增量消费方时自动退回一次性请求")


def test_provider_stream_fallback_when_server_ignores_stream() -> None:
    print("test_provider_stream_fallback_when_server_ignores_stream")
    # force_json=True：服务端忽略 stream 参数，直接返回 JSON
    with FakeLLM([text_response("服务端不支持流式")], force_json=True) as llm:
        provider = _provider(llm.base_url)
        deltas: list[str] = []
        resp = provider.chat([{"role": "user", "content": "hi"}], on_delta=deltas.append)
        if resp.content != "服务端不支持流式":
            _fail(f"回退解析失败：{resp.content!r}")
        if deltas:
            _fail("回退路径不应产生增量回调")
        _pass("服务端忽略 stream 时按一次性响应正确回退")


def test_provider_http_error_raises() -> None:
    print("test_provider_http_error_raises")
    with FakeLLM([http_error_response(401, "invalid api key")]) as llm:
        provider = _provider(llm.base_url)
        try:
            provider.chat([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
        except ProviderError as exc:
            if exc.retryable:
                _fail("401 不该被标记为可重试（重试没有意义）")
            _pass(f"HTTP 401 正确抛出且不可重试：{str(exc)[:60]}")
            return
        _fail("HTTP 错误没有被抛出")


def main() -> int:
    print(f"\n流式解析测试\n{'=' * 52}")
    cases = [
        test_iter_sse_json,
        test_merge_tool_name,
        test_accumulator_text,
        test_accumulator_tool_call_fragmented,
        test_accumulator_parse_failure_is_preserved,
        test_accumulator_multiple_tool_calls,
        test_provider_streaming_end_to_end,
        test_provider_streaming_tool_call,
        test_provider_no_stream_when_no_consumer,
        test_provider_stream_fallback_when_server_ignores_stream,
        test_provider_http_error_raises,
    ]
    passed = 0
    try:
        for fn in cases:
            fn()
            passed += 1
    except AssertionError:
        print(f"\n失败。已通过 {passed}/{len(cases)} 个用例。")
        return 1
    print(f"{'=' * 52}\n全部 {passed} 个用例通过 ✅\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

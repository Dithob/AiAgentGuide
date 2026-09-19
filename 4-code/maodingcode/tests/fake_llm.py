"""本地假模型服务。

存在的理由：**不花钱、不需要 API Key，也能验证完整链路。**

它是个真的 HTTP 服务，监听 127.0.0.1 的随机端口，实现
`POST /v1/chat/completions`。测试只需要把 `AI_BASE_URL` 指过来，
就能跑通「Loop → Provider → HTTP → SSE 解析 → 工具执行 → 回灌」整条链路。

比在进程内塞一个假 Provider 强的地方：它顺带验证了
HTTP 层、流式分片、Content-Type 嗅探、重试路径这些真会出问题的地方。

用法：

    llm = FakeLLM([tool_call_response("write_file", {...}), text_response("done")])
    llm.start()
    ...  # 把 AI_BASE_URL 指向 llm.base_url
    llm.stop()
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# --------------------------------------------------------------------- 构造器
def text_response(content: str, *, prompt_tokens: int = 100, completion_tokens: int = 20) -> dict:
    """一条普通的文本回复。"""
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def tool_call_response(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "call_fake_1",
    prompt_tokens: int = 120,
    completion_tokens: int = 30,
) -> dict:
    """一条工具调用回复。"""
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def http_error_response(status: int, message: str) -> dict:
    """让假服务返回一个 HTTP 错误状态码，用来测错误分支。"""
    return {"__status__": status, "__body__": {"error": {"message": message, "type": "fake_error"}}}


# --------------------------------------------------------------------- SSE 转换
def split_pieces(text: str, size: int = 4) -> list[str]:
    """把字符串切成小片，模拟流式的分片边界。

    分片是最容易出 bug 的地方（工具参数被切开），所以默认切得很碎。
    """
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def to_sse_chunks(response: dict, *, text_fragment: int = 3, arg_fragment: int = 5) -> list[dict]:
    """把一条完整响应拆成流式分片序列。

    默认切得很碎（正文 3 字符、工具参数 5 字符一片），
    这样短回复和多字节中文也能产生足够多的分片，断言才有意义；
    同时工具参数被切开是跨分片装配最容易出错的地方，必须覆盖到。
    """
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    chunks: list[dict] = []

    for piece in split_pieces(message.get("content") or "", text_fragment):
        chunks.append({"choices": [{"index": 0, "delta": {"content": piece}}]})

    for idx, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        # 第一个分片带 id 和工具名
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": tc.get("id"),
                                    "type": "function",
                                    "function": {"name": fn.get("name"), "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            }
        )
        # 后续分片只带被切碎的 arguments
        for frag in split_pieces(str(fn.get("arguments") or ""), arg_fragment):
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [{"index": idx, "function": {"arguments": frag}}]
                            },
                        }
                    ]
                }
            )

    chunks.append(
        {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}
            ]
        }
    )
    # usage 只在最后一片出现，且是独立的 chunk（OpenAI 的约定）
    chunks.append({"choices": [], "usage": response.get("usage") or {}})
    return chunks


# --------------------------------------------------------------------- 服务
class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeLLM/1.0"

    def log_message(self, *_args: Any) -> None:  # 静音，别污染测试输出
        return

    def do_POST(self) -> None:  # noqa: N802
        state: "FakeLLM" = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}

        state.requests.append({"path": self.path, "body": body})

        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": {"message": f"unknown path {self.path}"}})
            return

        entry = state.next_response()
        if entry is None:
            self._json(500, {"error": {"message": "FakeLLM 剧本用尽"}})
            return

        # 特殊：脚本项可以要求直接返回错误状态
        if "__status__" in entry:
            self._json(int(entry["__status__"]), entry["__body__"])
            return

        wants_stream = bool(body.get("stream")) and not state.force_json
        if wants_stream:
            self._sse(entry)
        else:
            self._json(200, entry)

    # -------------------------------------------------------------- 输出
    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, response: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for chunk in to_sse_chunks(response):
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.001)  # 制造真实的分片到达间隔
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端主动断开（测中断时会这样）


class FakeLLM:
    """假模型服务。

    Args:
        script: 依次返回的响应列表，每项是一条完整的 chat.completion 响应体。
            用尽之后返回 500，用来暴露"循环次数超出预期"。
        force_json: True 时即使请求要 stream 也返回普通 JSON，
            用来验证 Provider 的 Content-Type 嗅探回退。
    """

    def __init__(self, script: list[dict], *, force_json: bool = False) -> None:
        self.script = list(script)
        self.force_json = force_json
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -------------------------------------------------------------- 生命周期
    def start(self) -> "FakeLLM":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeLLM":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # -------------------------------------------------------------- 访问
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self.script)

    def next_response(self) -> dict | None:
        with self._lock:
            if not self.script:
                return None
            return self.script.pop(0)

    def last_request(self) -> dict[str, Any] | None:
        return self.requests[-1] if self.requests else None


__all__ = [
    "FakeLLM",
    "text_response",
    "tool_call_response",
    "http_error_response",
    "to_sse_chunks",
    "split_pieces",
]

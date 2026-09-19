"""OpenAI 兼容 Provider。

市面上绝大多数模型服务（DeepSeek、智谱、通义、火山、Ollama、vLLM……）
都提供 /chat/completions 接口，所以一个适配器就够覆盖一大片。

这里刻意不引入官方 SDK：一是不想被 SDK 的大版本升级绑架，
二是自己拼 payload 才能把"我们到底发了什么给模型"讲清楚。
如果你更想用 SDK，把 _request() 换掉即可，上层无感。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

from ..config import ProviderConfig
from ..errors import ConfigError, ProviderError
from .base import ChatResponse, Provider, ToolCallRequest, Usage
from .streaming import StreamAccumulator, iter_sse_json, parse_usage

# 这些状态码值得退避重试，其余基本是请求本身写错了
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatProvider(Provider):
    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self._endpoint = cfg.base_url.rstrip("/") + "/chat/completions"

    # ------------------------------------------------------------------ 对外
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        tool_list = list(tools or [])
        if tool_list:
            payload["tools"] = tool_list
            payload["tool_choice"] = "auto"

        # 只有"配置开了流式"且"调用方确实想要增量"时才走 SSE。
        # 否则保持一次性请求——流式会让错误处理和重试都变复杂，
        # 没有消费方的时候不该付这个代价。
        if on_delta is not None and self.cfg.stream:
            return self._request_stream(payload, on_delta)

        data = self._request_with_retry(payload)
        return self._parse(data)

    def supports_streaming(self) -> bool:
        return bool(self.cfg.stream)

    def count_tokens(self, text: str) -> int:
        """粗估：中文约 1 字 1 token，英文约 4 字符 1 token。

        不引入 tiktoken 是权衡后的选择——上下文裁剪只需要"量级正确"，
        而多一个二进制依赖会让项目在别人的机器上更难跑起来。
        """
        if not text:
            return 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return cjk + max(1, other // 4)

    def close(self) -> None:
        return None

    # ------------------------------------------------------------------ 内部
    def _request_with_retry(self, payload: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self._request(payload)
            except ProviderError as exc:
                last = exc
                if not exc.retryable or i == attempts - 1:
                    raise
                # 指数退避：0.5s -> 1s -> 2s
                time.sleep(0.5 * (2**i))
        raise ProviderError(f"模型调用失败：{last}", retryable=True)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise ProviderError(
                f"HTTP {exc.code}：{detail}",
                retryable=exc.code in RETRYABLE_STATUS,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"网络不可达：{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise ProviderError("请求超时", retryable=True) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"响应不是合法 JSON：{raw[:300]}") from exc

    # ------------------------------------------------------------------ 流式
    def _request_stream(
        self, payload: dict[str, Any], on_delta: Callable[[str], None]
    ) -> ChatResponse:
        """SSE 流式请求。

        与一次性请求的两点不同：

        1. **不重试**。流已经开始吐字节之后再重试，会把前半段正文重复一遍，
           用户看到的内容就乱了。宁可失败，让用户自己决定要不要重发。
        2. **要兼容"服务端不认流式"的情况**。部分中转或兼容层会忽略
           `stream: true`，直接返回一个普通 JSON；所以要嗅探 Content-Type，
           是 JSON 就按一次性响应处理。
        """
        payload = dict(payload)
        payload["stream"] = True
        # 只有带上这个，多数服务才会在最后一个分片里给 usage。
        # 不给也不会出错，只是这一轮的成本统计会缺数据。
        payload["stream_options"] = {"include_usage": True}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Accept": "text/event-stream",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=self.cfg.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise ProviderError(
                f"HTTP {exc.code}：{detail}",
                retryable=exc.code in RETRYABLE_STATUS,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"网络不可达：{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise ProviderError("请求超时", retryable=True) from exc

        with resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "event-stream" not in content_type:
                # 服务端没走流式，退回一次性解析
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return self._parse(json.loads(raw))
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"响应不是合法 JSON：{raw[:300]}") from exc

            acc = StreamAccumulator()
            try:
                for chunk in iter_sse_json(resp):
                    delta = acc.feed(chunk)
                    if delta:
                        try:
                            on_delta(delta)
                        except Exception:
                            # 回调方出问题不该影响取数据；静默降级为不推送
                            on_delta = lambda _t: None  # noqa: E731
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # 已经有内容就不要整段丢弃，把拿到的返回去，让上层判断
                if acc.content_parts or acc._tool_slots:
                    partial = acc.result()
                    partial.finish_reason = partial.finish_reason or "stream_interrupted"
                    return partial
                raise ProviderError(f"流式读取中断：{exc}", retryable=True) from exc

        return acc.result()

    def _parse(self, data: dict[str, Any]) -> ChatResponse:
        if "error" in data and not data.get("choices"):
            raise ProviderError(str(data["error"]))

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"响应中没有 choices 字段：{str(data)[:300]}")

        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):  # 部分服务返回分段内容
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        calls: list[ToolCallRequest] = []
        for item in msg.get("tool_calls") or []:
            fn = item.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(args, dict):
                    args = {"_value": args}
                parsed_ok = True
            except json.JSONDecodeError:
                args, parsed_ok = {}, False
            calls.append(
                ToolCallRequest(
                    id=item.get("id") or f"call_{len(calls)}",
                    name=fn.get("name") or "",
                    arguments=args,
                    raw_arguments=raw_args if not parsed_ok else "",
                )
            )

        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=parse_usage(data.get("usage") or {}),
            finish_reason=choice.get("finish_reason") or "",
            raw=data,
        )


def build_provider(cfg: ProviderConfig) -> Provider:
    """工厂。以后接 Anthropic / Gemini 原生协议就在这里分流。"""
    if not cfg.base_url:
        raise ConfigError("provider.base_url 不能为空")
    return OpenAICompatProvider(cfg)

"""MCP（Model Context Protocol）客户端。

解决的问题：工具不该写死在 Agent 里。文件系统、数据库、浏览器、公司内部系统……
每接一个都改一遍 Agent 代码是不可持续的。

MCP 把这件事标准化：Server 端用协议自描述"我有哪些工具"，
Client 端启动时拉取 tools/list，动态注册成本地工具。Agent 完全不知道
这些工具是内置的还是外挂的。

本实现覆盖两种传输：
    stdio —— 把 Server 当子进程，用 stdin/stdout 收发 JSON-RPC（最常用）
    http  —— 连远端 /mcp 端点（SSE 风格），用 POST 发 JSON-RPC

只实现 Agent 需要的最小方法集：
    initialize / tools/list / tools/call / ping
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

from ..config import MCPServerConfig
from ..errors import MCPError
from ..security.policy import Reversibility
from ..tools.base import Tool

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "maodingcode", "version": "0.1.0"}
DEFAULT_TIMEOUT = 30.0


@dataclass
class MCPClient:
    """一个 MCP Server 连接。"""

    config: MCPServerConfig
    timeout: float = DEFAULT_TIMEOUT

    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)
    _tools: list[dict[str, Any]] = field(default_factory=list, init=False)
    _initialized: bool = field(default=False, init=False)

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """建立连接并完成握手。失败抛 MCPError，由上层决定是否跳过这个 Server。"""
        if self.config.transport == "stdio":
            self._start_stdio()
        try:
            self._initialize()
            self._tools = self._fetch_tools()
            self._initialized = True
        except MCPError:
            self.close()
            raise

    def _start_stdio(self) -> None:
        cmd = [self.config.command, *self.config.args]
        if not shutil.which(self.config.command):
            raise MCPError(f"MCP Server 命令不存在：{self.config.command}")
        env = {**os.environ, **self.config.env}
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise MCPError(f"启动 MCP Server 失败：{exc}") from exc

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    # ------------------------------------------------------------------ 协议
    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}, "tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        server = (result or {}).get("serverInfo", {}) if isinstance(result, dict) else {}
        if server:
            self.config.env.setdefault("_server_name", str(server.get("name", "")))
        self._notify("notifications/initialized", {})

    def _fetch_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        if not isinstance(result, dict):
            return []
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            raise MCPError("tools/list 返回结构不符合预期")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return _render_tool_result(result)

    def ping(self) -> bool:
        try:
            self._request("ping", {})
            return True
        except MCPError:
            return False

    # ---------------------------------------------------------------- 传输实现
    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        response = self._roundtrip(message)
        if "error" in response:
            err = response["error"] or {}
            raise MCPError(f"{method} 失败：{err.get('message', err)}")
        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.config.transport == "stdio":
            self._write_line(json.dumps(message))
        # HTTP 传输下的通知可省略，多数实现容错

    def _roundtrip(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.config.transport == "stdio":
            return self._roundtrip_stdio(message)
        return self._roundtrip_http(message)

    def _roundtrip_stdio(self, message: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPError("MCP Server 未启动")
        with self._lock:  # JSON-RPC 是一问一答，必须串行化
            self._write_line(json.dumps(message))
            target_id = message.get("id")
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise MCPError("MCP Server 已关闭连接")
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Server 可能往 stdout 打了日志，忽略
                if payload.get("id") == target_id:
                    return payload
                # 其他 id 的消息（如 Server 主动发的请求）本实现忽略

    def _write_line(self, text: str) -> None:
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write(text + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise MCPError(f"写入 MCP Server 失败：{exc}") from exc

    def _roundtrip_http(self, message: dict[str, Any]) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        body = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code}：{exc.read()[:300]!r}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MCPError(f"连接 MCP Server 失败：{exc}") from exc

        payload = _parse_maybe_sse(raw)
        if payload is None:
            raise MCPError(f"无法解析 MCP 响应：{raw[:200]}")
        return payload

    # ------------------------------------------------------------------ 转工具
    def as_tools(self) -> list[Tool]:
        """把远端工具包装成本地 Tool，注册后 Agent 侧完全无感。"""
        out: list[Tool] = []
        for spec in self._tools:
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
            annotations = spec.get("annotations") or {}
            read_only = bool(annotations.get("readOnlyHint", False))

            out.append(
                Tool(
                    name=f"mcp__{self.config.name}__{name}",
                    description=str(spec.get("description") or f"MCP 工具 {name}"),
                    parameters=schema,
                    handler=self._make_handler(name),
                    read_only=read_only,
                    reversibility=(
                        Reversibility.READ_ONLY if read_only else Reversibility.IRREVERSIBLE
                    ),
                    source=f"mcp:{self.config.name}",
                )
            )
        return out

    def _make_handler(self, remote_name: str):
        def handler(**kwargs: Any) -> str:
            return self.call_tool(remote_name, kwargs)

        return handler

    @property
    def tool_names(self) -> list[str]:
        return [str(t.get("name", "")) for t in self._tools]


def _render_tool_result(result: Any) -> str:
    """把 MCP 的 content 数组转成纯文本。"""
    if result is None:
        return "(无输出)"
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)

    chunks: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            chunks.append(str(item.get("text", "")))
        elif kind == "resource":
            chunks.append(f"[资源] {item.get('resource', {})}")
        elif kind == "image":
            chunks.append(f"[图片 {item.get('mimeType', 'unknown')}]")
        else:
            chunks.append(json.dumps(item, ensure_ascii=False))
    if not chunks and result.get("structuredContent") is not None:
        chunks.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    if result.get("isError"):
        return "MCP 工具报错：" + ("\n".join(chunks) or "(无详情)")
    return "\n".join(chunks) or "(无输出)"


def _parse_maybe_sse(raw: str) -> dict[str, Any] | None:
    """兼容 text/event-stream：取最后一个 data: 行解析。"""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    return None


@dataclass
class MCPManager:
    """管理一组 MCP Server，负责启动、失败隔离与工具汇总。"""

    clients: list[MCPClient] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_configs(cls, servers: list[MCPServerConfig], timeout: float = DEFAULT_TIMEOUT) -> "MCPManager":
        manager = cls()
        for cfg in servers:
            if not cfg.enabled:
                continue
            client = MCPClient(config=cfg, timeout=timeout)
            try:
                client.start()
            except MCPError as exc:
                # 单个 Server 挂掉不能拖垮整个 Agent。
                # 记录错误，让 /mcp 命令能看到，但继续启动。
                manager.errors[cfg.name] = str(exc)
                continue
            manager.clients.append(client)
        return manager

    def tools(self) -> list[Tool]:
        out: list[Tool] = []
        for c in self.clients:
            out.extend(c.as_tools())
        return out

    def close(self) -> None:
        for c in self.clients:
            c.close()
        self.clients.clear()

    def describe(self) -> str:
        if not self.clients and not self.errors:
            return "未配置 MCP Server"
        lines = []
        for c in self.clients:
            lines.append(
                f"[已连接] {c.config.name}（{c.config.transport}）"
                f" 工具 {len(c.tool_names)} 个：{', '.join(c.tool_names) or '无'}"
            )
        for name, err in self.errors.items():
            lines.append(f"[失败]   {name}：{err}")
        return "\n".join(lines)


__all__ = ["MCPClient", "MCPManager", "PROTOCOL_VERSION"]

# 05 · MCP 接入

代码：`maodingcode/mcp/client.py`

## 解决什么问题

工具不该写死在 Agent 里。文件系统、数据库、浏览器、公司内部系统……
每接一个都改一遍 Agent 代码是不可持续的。

MCP（Model Context Protocol）把这件事标准化：Server 端自描述"我有哪些工具"，
Client 端启动时拉取工具清单，动态注册成本地工具。**Agent 完全不知道这些工具是内置的还是外挂的。**

## 握手时序

```
Client                                          Server
  │                                                │
  ├── initialize {protocolVersion, clientInfo} ──▶ │
  │ ◀── {serverInfo, capabilities}                 │
  ├── notifications/initialized ────────────────▶ │   （通知，无需响应）
  ├── tools/list ───────────────────────────────▶ │
  │ ◀── {tools: [{name, description, inputSchema}]}│
  │                                                │
  ├── tools/call {name, arguments} ─────────────▶ │
  │ ◀── {content: [{type:"text", text:"..."}]}     │
```

`PROTOCOL_VERSION = "2024-11-05"` 是握手时声明的协议版本。

## 两种传输

### stdio —— 把 Server 当子进程

```toml
[[mcp.servers]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
```

Client 用 `subprocess.Popen` 起进程，通过 stdin/stdout 收发 JSON-RPC。
这是最常用的方式，因为本地工具天然适合进程隔离。

**JSON-RPC 必须串行化。** 一问一答的模型下，并发写 stdin 会让响应错配：

```python
with self._lock:
    self._write_line(json.dumps(message))
    while True:
        line = self._proc.stdout.readline()
        ...
```

**要容忍 Server 往 stdout 打日志。** 有些实现会把日志和协议消息混着输出，
所以读到的每一行都先尝试 `json.loads`，解析失败的直接跳过，
而不是报错退出：

```python
except json.JSONDecodeError:
    continue   # Server 可能往 stdout 打了日志，忽略
```

### HTTP —— 连远端端点

```toml
[[mcp.servers]]
name = "remote-tools"
url = "https://your-mcp-server.example.com/mcp"
```

POST JSON-RPC，响应可能是纯 JSON 也可能是 SSE 格式（`text/event-stream`）。
`_parse_maybe_sse()` 两种都处理：先试纯 JSON，失败则从后往前找 `data:` 行。

## 动态注册成本地工具

这是整个模块的核心价值所在：

```python
def as_tools(self) -> list[Tool]:
    for spec in self._tools:
        out.append(Tool(
            name=f"mcp__{self.config.name}__{name}",   # 命名空间防冲突
            description=...,
            parameters=spec.get("inputSchema"),
            handler=self._make_handler(name),          # 闭包转发
            read_only=annotations.get("readOnlyHint", False),
            source=f"mcp:{self.config.name}",
        ))
```

三个细节：

1. **命名空间前缀 `mcp__<server>__<tool>`**：不同 Server 可能有同名工具，
   加前缀避免注册时冲突。同时 `by_source("mcp:")` 可以筛出所有 MCP 工具。
2. **`readOnlyHint` 注解**：MCP 协议允许 Server 声明工具是否只读。
   认这个注解，只读工具就能免确认直接跑，写类工具走权限确认。
   没声明的一律按 `IRREVERSIBLE` 处理（保守）。
3. **闭包转发**：本地 `Tool.handler` 只是把调用转发给 `call_tool()`，
   Agent 侧完全无感。

## 失败隔离

```python
for cfg in servers:
    try:
        client.start()
    except MCPError as exc:
        manager.errors[cfg.name] = str(exc)   # 记下来
        continue                              # 不中断启动
```

一个 MCP Server 拉不起来（命令不存在、网络不通、依赖缺失），
**不能让整个 Agent 起不来**。错误记进 `MCPManager.errors`，
用户用 `/mcp` 能看到是哪个 Server 挂了、为什么。

理由：MCP Server 是第三方依赖，可用性不受你控制。
用户装了一个用不上的 Server 就完全没法工作，是不能接受的。

## 结果渲染

MCP 返回的是 content 数组，可能是文本、图片、资源引用：

```python
for item in result.get("content") or []:
    if kind == "text":     chunks.append(text)
    elif kind == "resource": chunks.append(f"[资源] {resource}")
    elif kind == "image":  chunks.append(f"[图片 {mimeType}]")
```

`isError` 为真时前缀 `MCP 工具报错：`，让模型知道这是失败而不是正常输出。

## 调试建议

```bash
# 1. 先确认 Server 本身能起来
npx -y @modelcontextprotocol/server-filesystem /tmp

# 2. 再用 /mcp 看连接状态
> /mcp
[已连接] filesystem（stdio） 工具 12 个：read_file, write_file, ...
[失败]   remote-tools：连接 MCP Server 失败：<urlopen error timed out>
```

如果工具数超过阈值触发了索引模式导致模型找不到 MCP 工具，
用 `list_tools` 检索，或调大 `index_mode_threshold`。

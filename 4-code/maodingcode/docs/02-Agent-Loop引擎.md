# 02 · Agent Loop 引擎

代码：`maodingcode/core/loop.py`

## 骨架

```python
while step < max_steps:
    resp = provider.chat(ctx.build(...))
    if not resp.wants_tools:
        return resp.content              # ① 收敛
    for call in resp.tool_calls:
        decision = policy.check(call.name, call.arguments)
        result = registry.dispatch(call.name, call.arguments)
        ctx.add(Message.tool_result(...))
```

这个 while 谁都会写。**真正难的是让它在异常情况下也能停下来。**

## 四个退出条件

| 退出条件 | 触发场景 | StopReason |
|---|---|---|
| 模型不再要求工具调用 | 正常完成 | `COMPLETED` |
| 达到 `max_steps` | 任务太复杂或模型在原地打转 | `MAX_STEPS` |
| 用户按 Ctrl+C | 主动中断 | `ABORTED` |
| 同一调用连续失败 3 次 | 工具坏了，重试没意义 | `FAILURE_LOOP` |

第 3 个（用户中断）通过 `abort()` 标志实现：UI 在线程外调用它，
循环在下一轮开始时检查并退出。之所以不做成强制 kill，
是因为工具执行到一半被打断可能留下半成品文件——
让循环在**轮次边界**退出，状态始终是干净的。

第 4 个（失败熔断）是最容易被漏掉的。没有它会出现这种情形：

```
模型: read_file("a.txt")  → 文件不存在
模型: read_file("a.txt")  → 文件不存在
模型: read_file("a.txt")  → 文件不存在     ← 烧钱
...
```

熔断键是 `工具名 + 排序后的参数`。参数变了就说明模型在尝试别的路径，重置计数。
连续 3 次完全一样的失败调用才熔断，避免误杀。

## 事件流

引擎不打印任何东西，只发事件：

```python
LoopEvent(kind="step_start",      payload={step, stats})
LoopEvent(kind="model_response",  payload={content, tool_calls, usage, elapsed})
LoopEvent(kind="tool_start",      payload={name, args})
LoopEvent(kind="tool_result",     payload={name, ok, output, elapsed})
LoopEvent(kind="assistant_message" / "error" / "stopped")
```

`ui/render.py:render_event()` 是唯一把事件翻译成人类可见文本的地方。
换成 Web UI 就换这一个函数，引擎和策略一行都不用改。

这也是为什么这个项目能被测试：`tests/smoke_test.py` 传入一个
`emit=lambda e: events.append(e.kind)`，就能断言事件流完整性。

## 工具调用的完整处理链

`_execute_one()` 里有四道关，顺序不能变：

```
1. 参数 JSON 解析失败？
   → 把原始字符串回灌，让模型自己修 JSON
   （不抛异常：模型下一轮通常就改对了，不值得中断整个任务）

2. Policy.check() → DENY？
   → 把拒绝原因回灌，模型会换方案

3. Policy.check() → ASK？
   → 交给 UI 确认；UI 没实现确认机制时默认拒绝（fail-closed）
   → 用户拒绝后回灌"用户拒绝了这次调用"，让模型问用户想怎么做

4. 执行
   → 工具抛任何异常都在这里被兜住，转成文本回灌
   → 工具异常绝不能掀翻整个循环
```

第 1 步和第 4 步的设计哲学是一样的：**把工具层的失败降级成"给模型的一条信息"**，
而不是"给用户的一个崩溃"。模型拿到错误后重试的正确率相当高，
这比把控制权交回用户更省事。

## 为什么工具结果永远返回字符串

`Tool.call()` 强制把返回值转成字符串，包括 None → `"(无输出)"`。

理由：如果让工具返回 dict，每个调用点都要自己序列化，
而且很容易漏掉 `ensure_ascii=False` 导致中文变成 `\u4e2d\u6587`——
模型看到转义序列的理解能力会下降。统一在一个地方序列化，
配合 `tests/smoke_test.py` 里对中文输出的断言，这类问题不会再出现。

## 上下文里必须保留"模型自己说过的话"

回灌工具结果时，除了 `tool` 消息，还必须先把 assistant 的
`tool_calls` 原文加进历史：

```python
self.context.add(
    Message.assistant(content=response.content,
                      tool_calls=build_tool_calls_payload(response.tool_calls))
)
```

而且 `build_tool_calls_payload()` 优先使用 `raw_arguments`
（模型当时发出的原始 JSON 字符串），而不是重新序列化一遍解析结果。

原因：多数模型服务会校验历史里 `tool_calls` 与 `tool` 消息的配对关系。
如果参数被重新序列化后格式略有差异，某些严格实现会返回 400。

## 可调参数

| 参数 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `max_steps` | `config.agent.max_steps` | 25 | 单轮最大步数 |
| `FAILURE_STREAK_LIMIT` | `loop.py` | 3 | 同一调用连续失败几次熔断 |
| `keep_recent_turns` | `config.agent` | 6 | 最近几轮不参与上下文折叠 |

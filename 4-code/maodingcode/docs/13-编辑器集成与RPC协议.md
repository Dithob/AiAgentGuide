# 13 · 编辑器集成与 RPC 协议

代码：`maodingcode/rpc.py`、`session.py`、`vscode-extension/`

## 为什么要做第三种入口

项目一开始只有 CLI。加编辑器插件的时候，第一个念头是"用 JS 重写一遍 Agent"。
这个念头必须掐死，理由是：

| | 重写一遍 | 复用同一份引擎（本项目的做法） |
|---|---|---|
| 行为一致性 | 两个实现，两套 bug | 天然一致 |
| 加个功能 | 两边各改一次 | 只改引擎 |
| 模型/权限/技能配置 | 两处配置，容易漂移 | 一处配置，两边生效 |
| 前端崩溃 | 状态丢失 | Agent 状态还在 |

所以架构是：**引擎一份（Python），外壳三个（CLI / RPC / VSCode 插件）。**

```
        ┌─────────────────────────┐
        │   Session（装配层）       │
        │  policy/registry/skills  │
        │  context/provider/memory │
        └───────────┬─────────────┘
                    │  make_loop(emit, confirm)
        ┌───────────▼─────────────┐
        │      AgentLoop           │  只发事件，不打印
        └───────────┬─────────────┘
        ┌───────────┼───────────┬──────────────┐
        ▼           ▼           ▼              ▼
     ChatCli     RpcServer   ScriptedProvider  测试
     (终端)      (编辑器)     (离线测试)
```

能不能做到这一点，取决于装配层有没有被抽干净。
**最初 `Session` 是写在 `cli.py` 里的**，加 RPC 时就得把装配逻辑抄一遍——
抄的那一刻开始，两条路径就会慢慢分叉（权限回填漏了、技能索引忘了刷新……）。
所以这次重构把它挪进了 `session.py`。

## NDJSON 协议

`python -m maodingcode --rpc`

### 为什么不用 HTTP

编辑器插件要的是"双向流"：前端推命令进去、后端推事件出来。用 HTTP 就得做
SSE + 轮询两条路，还要处理端口占用、鉴权、进程孤儿。NDJSON over stdio 天然具备：

- 双向、有序、**不需要端口**；
- 进程随编辑器窗口存亡，**不留孤儿进程**；
- **没有网络暴露面**，不用做鉴权（只有父进程能读到这根管道）。

### 命令（前端 → 后端，stdin）

| 命令 | 参数 | 说明 |
|---|---|---|
| `run` | `prompt`, `allow_all?` | 跑一轮任务 |
| `abort` | — | 请求中断（轮次边界生效） |
| `confirm_response` | `id`, `allow`, `all?` | 回答权限确认 |
| `tools` / `skills` / `mcp` / `policy` | — | 自省，回 `info` |
| `config` | — | 脱敏配置，回 `config` |
| `cost` | — | 本轮统计，回 `cost` |
| `clear` | — | 清空对话历史 |
| `memory` | `action`: read/init/append, `note?` | 项目约定读写 |
| `ping` / `shutdown` | — | 存活检查 / 退出 |

### 事件（后端 → 前端，stdout）

| 事件 | 关键字段 | 前端用它做什么 |
|---|---|---|
| `ready` | `model`, `workspace`, `permission_mode`, `streaming`, `tools`, `skills` | 顶栏显示状态 |
| `delta` | `text` | **逐字追加到当前气泡** |
| `step_start` | `step`, `stats` | 显示"第 N 步" |
| `model_response` | `content`, `tool_calls[]`, `usage`, `elapsed` | 收尾当前气泡 |
| `tool_start` | `name`, `args` | 插一张"运行中"的工具卡 |
| `tool_result` | `name`, `ok`, `output`, `elapsed` | 更新那张卡（成功折叠 / 失败展开） |
| `needs_confirm` | `id`, `tool`, `args`, `reason` | 弹确认卡，三个按钮 |
| `assistant_message` | `text` | 非流式模式下补正文 |
| `done` | `reason`, `content`, `steps`, `tool_calls`, `cost` | 解锁输入框 + 显示统计 |
| `error` / `warning` / `info` | `message` | 提示条 |
| `fatal` | `message` | 启动失败（配置错、缺 Key） |
| `exited` | `code`, `signal` | 子进程挂了，提示重启 |

## 两个必须讲清楚的实现点

### 1. 线程模型：为什么 worker 是独立线程

`loop.run()` 是同步阻塞的。如果它在主线程跑，主线程就被占住，
`abort` 和 `confirm_response` **永远读不到**——权限确认会直接死锁。

所以：

```
主线程：for line in stdin  →  dispatch 命令   （永远在等输入）
worker：loop.run(prompt)  →  emit 事件        （跑一轮任务）
```

权限确认的跨线程握手：

```
worker                                 主线程
  │ 发 needs_confirm {id}                 │
  │ 阻塞在 Event.wait()  ◀─────────────   │ 收到 {"cmd":"confirm_response", id, allow}
  │                                      │ 写入结果 → Event.set()
  │ 被唤醒 → 返回 allow                   │
```

超时 300 秒按**拒绝**处理（fail-closed）——不能因为前端不说话就放行动作。

### 2. stdout 卫生：协议流不能被 print 污染

stdout 被协议独占了。任何一句意外的 `print()` 都会让前端收到一行
解析不了的垃圾。所以 `RpcServer.serve()` 启动时：

```python
saved_stdout = sys.stdout
sys.stdout = sys.stderr      # 后面所有 stray print 都去 stderr
self._out = saved_stdout     # 协议只写这个句柄
```

另外 `send()` 带锁——worker 和后端线程都可能发事件，并发写会撕裂行。

## 插件侧（vscode-extension/）

纯 JavaScript，**没有构建步骤**。这是刻意的：扩展本身只有几百行，
加一套 TypeScript 构建链会让"改一行 → 重新打包 → 重载"变得很重。

```
vscode-extension/
├── package.json        清单：视图容器 / 命令 / 配置项
├── extension.js        主进程：拉进程、转协议、转事件
├── media/
│   ├── main.js         Webview：渲染聊天气泡
│   ├── style.css       全部走 VSCode 主题变量，浅深色自动适配
│   └── icon.svg        活动栏图标
└── .vscodeignore
```

### 数据流

```
Webview ──postMessage──▶ extension.js ──NDJSON(stdio)──▶ python --rpc
   ▲                          │
   └──────postMessage─────────┘
```

`extension.js` 里有三个值得注意的处理：

1. **按行切 stdout** —— `data` 事件不保证一次是一整条 JSON，
   必须维护 buffer 按 `\n` 切。直接 `JSON.parse(chunk)` 在长输出下必崩。
2. **事件缓存** —— 进程可能在 Webview 还没加载完就发了 `ready`。
   非 `delta` 事件先缓存，收到 `webviewReady` 后补发，
   避免"重载面板之后状态全丢"。
3. **`delta` 不入缓存** —— 增量事件量大且过时就没意义，缓存它们只会拖慢。

### 前端渲染的两个决定

- **不用 `innerHTML`**。内容来自模型和文件，直接拼 HTML 等于开了个注入口子。
  全部走 `textContent`，代码块用 `split(/```/)` 手工切分再建 `<pre>`。
- **成功的工具卡自动折叠，失败的一直展开**。正常流程不该刷屏，
  但出错必须让人一眼看到。默认全展开会让一次任务刷出几十屏。

### API Key 不放 settings.json

调用配置走 VSCode 的 settings（可版本化），但**密钥走工作区根目录的 `.env`**。
这样密钥不会进 `settings.json`（很多人会把它同步到云端），也不容易被误提交。

## 契约测试：跨语言的约定要靠测试钉死

JS 的 `switch (e.type)` 和 Python 的 `send(type=...)` 之间**没有编译器**。
改了一边的名字，结果只是"界面上某块永远不更新"，而且不报错。

所以 `tests/test_rpc.py::test_rpc_protocol_contract_for_frontend` 做了这件事：

```python
required = {"ready", "delta", "model_response", "tool_start", "tool_result",
            "needs_confirm", "done", "info", "error"}
# 真跑两轮会话，收集实际出现过的事件类型
missing = required - observed   # 少了任何一个就失败
```

同时校验 `needs_confirm` 带齐 `id/tool/args/reason`、
`done` 带齐 `reason/content/steps/tool_calls/cost`——
这些字段名前端写死了，改名同样是静默失败。

## 加一个新的前端能力，要动哪些地方

1. `rpc.py`：加命令处理函数 `_cmd_xxx`，或加事件 `send(type="xxx", ...)`
2. `test_rpc.py`：加进契约测试的 `required` 集合
3. `extension.js`：`_onMessage` 加分支（前端 → 后端）或转发事件
4. `media/main.js`：`switch` 加分支渲染

四步里第 2 步是最容易偷懒、也最不能偷懒的。

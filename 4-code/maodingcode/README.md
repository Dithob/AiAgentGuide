# MaoDingCode

本地运行的 AI 编码助手。一个可读、可改、可讲的 coding agent 参考实现。

核心只有一条链路：

```
用户输入
   ↓
上下文装配（系统提示 + 历史 + 工具 schema）
   ↓
模型调用 ──── 无工具调用 ──→ 输出最终回答
   ↓ 有工具调用
权限策略校验 → 工具执行 → 结果回灌 ──┐
   ↑                                │
   └────────────────────────────────┘
```

## 快速开始

> **先记住两条目录规则**（因为项目根与包目录拍平了，导入根在上一级）：
>
> | 想跑什么 | 在哪个目录下执行 |
> |---|---|
> | `python run_demo.py` · `python run_tests.py` | **本目录** `maodingcode/` |
> | `python -m maodingcode`（含 `--rpc`） | **上一级** `4-code/` |
>
> `.env` 也放在 `4-code/` —— 配置按「工作区根」读取（`config.py` 读 `<workspace>/.env`）。

### 先看效果（不用 API Key、不联网、不花钱）

```bash
python run_demo.py
```

它会**在本地起一个假模型服务**（真的 HTTP + 真的 SSE），把 Agent 指向它，
跑一个完整的多步任务：看目录 → 写文件 → 执行验证 → 汇报。

所以你看到的是真实链路，只是"大脑"是写死的剧本。想验证项目能不能跑，这一条就够了。

### 三种运行方式

```bash
cd ..                           # 切到 4-code/：它是 maodingcode 包的导入根

# ① 独立运行（终端交互）
cp .env.example .env            # 填 AI_API_KEY / AI_BASE_URL / AI_MODEL_NAME
python -m maodingcode

# ② 一次性执行（脚本 / CI）
python -m maodingcode "解释一下 maodingcode/tools/registry.py 里索引模式的作用"
python -m maodingcode --allow "把 README 里的错别字修一下"   # 自动放行确认

# ③ 协议模式（供编辑器插件驱动）
python -m maodingcode --rpc   # stdin 收 NDJSON 命令，stdout 发 NDJSON 事件

# ④ VSCode 插件
code maodingcode/vscode-extension && 按 F5   # 开发模式（纯 JS，无需 npm install）
```

要求 Python 3.11+（用到标准库 `tomllib`），**核心零第三方依赖**。

## 跑测试

```bash
python run_tests.py          # 一键跑全部（在本目录 maodingcode/ 下执行）
```

| 套件 | 项数 | 覆盖 |
|---|---|---|
| `tests/smoke_test.py` | 17 | Loop 收敛/熔断、上下文折叠与丢弃、权限三挡、工具、技能、配置合并 |
| `tests/test_streaming.py` | 11 | SSE 分片解析、跨分片工具调用装配、回退路径 |
| `tests/test_rpc.py` | 10 | 真子进程 + 真 HTTP + 真 SSE 的端到端，含跨语言协议契约 |

全部**离线可跑，不需要 API Key**。关键设计是 `AgentLoop` 只依赖 `Provider` 抽象，
换掉实现就能把整条链路放进测试里。

## 能力

| 能力 | 说明 | 代码 |
|---|---|---|
| Agent Loop | 感知→决策→行动→观察，含步数上限与失败熔断 | `core/loop.py` |
| 上下文工程 | 系统提示分区装配、token 预算、工具输出折叠、整轮丢弃 | `core/context.py` |
| 流式输出 | SSE 解析 + 跨分片工具调用装配 + 服务端不支持时自动回退 | `provider/streaming.py` |
| 工具系统 | 注册中心 + 参数校验 + 工具过多时自动切索引模式 | `tools/` |
| 权限沙箱 | 三挡权限模式、可逆性分级、路径越界检测、危险命令黑名单 | `security/policy.py` |
| MCP 接入 | stdio / HTTP 双传输，外部工具动态注册成本地工具 | `mcp/client.py` |
| Skills | `SKILL.md` 按需加载，索引常驻、正文懒加载 | `skills/loader.py` |
| 记忆系统 | 项目约定文件 + 会话快照 | `core/memory.py` |
| 模型接入 | 任何 OpenAI 兼容服务，手写 HTTP 无 SDK 依赖 | `provider/` |
| 可观测性 | 步数 / token / 缓存命中 / 成本 / 各阶段耗时 | `core/observability.py` |
| 编辑器集成 | NDJSON 协议 + VSCode 插件（纯 JS，无构建步骤） | `rpc.py`、`vscode-extension/` |


## 目录结构

本目录**既是项目根、也是 Python 包目录**（拍平，见下），外面套一层 `4-code/` 容器：

```
4-code/                       容器 = 导入根（`python -m maodingcode` 在这里跑）
├── .env.example              环境变量模板（两个 demo 共用）
├── maodingcode/              ← 本目录：项目根 == 包目录
│   ├── config.py             分层配置（默认值 < env < toml < 命令行）
│   ├── errors.py             异常体系
│   ├── session.py            装配层（三种宿主共用）
│   ├── cli.py                终端宿主：REPL + 斜杠命令
│   ├── rpc.py                协议宿主：NDJSON over stdio
│   ├── __main__.py           `python -m maodingcode` 入口
│   ├── provider/             模型接入层（含 SSE 流式）
│   ├── core/                 Loop / 上下文 / 记忆 / 可观测性
│   ├── tools/                工具抽象、注册中心、内置工具
│   ├── mcp/                  MCP 客户端与管理器
│   ├── skills/               技能加载器 + 示例技能 code-review/
│   ├── security/             权限策略与沙箱
│   ├── ui/                   终端渲染
│   ├── vscode-extension/     VSCode 插件（纯 JS，无构建步骤）
│   ├── docs/                 设计文档（13 篇）
│   ├── tests/                三个离线测试套件 + 本地假模型服务
│   ├── run_demo.py           零配置演示（不用 API Key）
│   ├── run_tests.py          一键跑全部测试
│   ├── maodingcode.toml.example  项目配置示例
│   └── requirements.txt
└── first_agent/              另一个 demo：早期练手代码（教学用，独立可跑）
```

**为什么拍平**：Windows 文件系统大小写不敏感，如果按常规写成
`4-code/MaoDingCode/maodingcode/`，外层目录与内层包名会被判定为同一个目录而建不出来。
把项目根与包目录合并成一层就绕开了这个问题，代价是**导入根上移到了 `4-code/`**——
所以 `python -m maodingcode` 必须在 `4-code/` 下执行（见「快速开始」）。

## 设计原则

1. **引擎不碰界面。** `AgentLoop` 只发事件（`LoopEvent`），一个字都不打印。
   终端渲染、Webview、测试收集器都是订阅方。
2. **装配只有一份。** 权限回填、技能索引这些隐性依赖都放在 `session.py`。
   三种宿主（CLI / RPC / 插件）共用它——装配逻辑一旦分叉，
   迟早在某条路径上漏掉一步（比如忘了刷新技能索引）。
3. **零第三方依赖。** 模型调用用 `urllib` 手写，`.env` 解析十行，TOML 用标准库。
   少一个依赖就少一类"在我这跑不了"。
4. **权限是确定性的。** 危险命令靠正则黑名单拦，不交给模型判断——模型可以说服自己任何事。
5. **错误要可自愈。** 工具报错时把"当前目录有什么""期望的 schema 长什么样"一起返回，
   让模型下一轮自己改对，而不是让用户重来。
6. **默认失败即拒绝。** 权限确认没有 UI 实现时默认拒绝，而不是静默放行。
7. **测试不依赖外部服务。** 本地假模型服务让整条链路（HTTP + SSE + 工具执行）
   都能离线验证，不需要 Key、不花钱。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/01-架构总览.md](docs/01-架构总览.md) | 分层、数据流、关键抽象 |
| [docs/02-Agent-Loop引擎.md](docs/02-Agent-Loop引擎.md) | 主循环与四个退出条件 |
| [docs/03-上下文工程.md](docs/03-上下文工程.md) | 系统提示装配与两级裁剪 |
| [docs/04-工具系统.md](docs/04-工具系统.md) | 工具设计、注册中心、索引模式 |
| [docs/05-MCP接入.md](docs/05-MCP接入.md) | 协议握手与动态工具注册 |
| [docs/06-Skills技能系统.md](docs/06-Skills技能系统.md) | 按需加载的提示词包 |
| [docs/07-记忆系统.md](docs/07-记忆系统.md) | 长期约定与会话快照 |
| [docs/08-权限与安全沙箱.md](docs/08-权限与安全沙箱.md) | 可逆性分级、路径沙箱、命令黑名单 |
| [docs/09-模型接入层.md](docs/09-模型接入层.md) | Provider 抽象与重试策略 |
| [docs/10-可观测性与成本.md](docs/10-可观测性与成本.md) | 埋点、指标与成本估算 |
| [docs/11-实现踩坑记录.md](docs/11-实现踩坑记录.md) | 开发中真实踩到的 5 个 bug |
| [docs/12-面试讲述手册.md](docs/12-面试讲述手册.md) | 怎么把这套东西讲清楚 |
| [docs/13-编辑器集成与RPC协议.md](docs/13-编辑器集成与RPC协议.md) | 协议设计、线程模型、插件架构 |
| [vscode-extension/README.md](vscode-extension/README.md) | 插件的安装、配置与排查 |

## 来源与致谢

本项目是在学习 **AI Agent 通识教程**（小傅哥，Apache-2.0 协议）的公开教学内容过程中
独立编写的，目的是理解 Agent 运行时的工程设计，而不是套用一个现成的 Agent 框架。

把这个项目的来源拆开说清楚：

**学到的** —— Agent 运行时的通用设计思路：Loop 的基本结构、工具按需暴露、
权限分级、上下文预算管理。这些属于公开讨论中的共识做法，不专属于任何一个项目。

**独立实现的** —— 本仓库的全部代码。技术路线也是分开的：
本项目是纯 Python、零第三方依赖、模型调用手写 HTTP；
而市面上的桌面端 coding agent 产品通常是 Tauri / Rust 外壳去桥接一个 CLI 内核，
两边没有可复用的代码。

**本仓库自己的实现决策** —— 两级上下文裁剪策略、可逆性驱动的权限模型、
手写 JSON-RPC 客户端、工具过多时的索引模式降级、失败熔断，
以及 `docs/11-实现踩坑记录.md` 里记录的四个真实 bug。

被问到"这和别的 coding agent 是什么关系"时，
`docs/12-面试讲述手册.md` 的最后一节给了完整的答法。

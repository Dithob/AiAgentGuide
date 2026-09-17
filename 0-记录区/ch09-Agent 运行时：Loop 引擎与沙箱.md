# 第 9 章 · Agent 运行时：Loop 引擎与沙箱

> **来源**：<https://ai-agent-guide.xiaofuge.cn/> → 第 9 章
> **记录日期**：2026-09-17　|　**重要度**：★★★　|　**掌握度**：

---

## 📌 记号约定（记录区专用）

| 记号         | 含义                               | 整理阶段我会怎么处理                              |
| :----------- | :--------------------------------- | :------------------------------------------------ |
| `==……==`     | **加强学习标记**——这个知识点要加深 | 知识库加「🔆 强化延伸」+ 派生测验题                |
| `~~A~~ B`    | 我选了 A（错），正确答案是 B       | 记进面试题库错题表 → 写「错题根治」→ 进测验错题池 |
| 原文摘录     | 我认为重要的原文，**原样贴**       | 保真收录进知识库「📌 你的原文记录」，不改写        |
| `> 疑问：……` | 我自己想到的问题                   | 知识库「❓ 你的疑问解答」正面回答 + 配自测题       |

> 记的时候别管格式好不好看。**记号是给整理阶段看的**，不是给别人看的。

---

## 🗺️ 一句话地图

> 2~3 句话说清本章主线：解决什么问题、和上一章什么关系、学完能回答什么。
> 目的是两个月后翻回来 5 秒想起整章。

---

## 📖 正文记录

> 直接从教程**原样摘录**你认为重要的表格、代码、段落。别改写、别总结——改写留给整理阶段。
> 判断标准：面试官问到这个，我答不上来会不会很难看？会 → 摘。

### 三层嵌套架构

🔄 Agent Loop（智能体执行循环）

LLM 驱动的感知→推理→规划→执行→观察→评估的6步循环。决定 Agent **做什么**——如何决策、迭代和自我纠正。

⚙️ Runtime（智能体运行时）

为 Loop 提供工具调用、记忆管理、多 Agent 协作、可观测性等基础设施。决定 Agent **怎么做**——把逻辑语义转化为可执行计算。

🔒 Sandbox（安全执行沙箱）

在代码执行、工具调用等不可信行为发生前，提供进程/容器/虚拟机级别的隔离保护。决定 Agent **在哪里做、在什么约束下做**。

三者呈现严格的嵌套关系——Sandbox 在最外层（所有 Agent 行为最终都在某个隔离环境中执行），Runtime 在中间层（为 Loop 提供运行所需的一切基础设施），Agent Loop 在最内层（是 Runtime 中的一个逻辑循环，调用工具和 LLM）。



### Agent Loop——智能体执行循环

| 步骤              | 职责                     | 典型输入                     | 典型输出                |
| :---------------- | :----------------------- | :--------------------------- | :---------------------- |
| **Perceive 感知** | 收集所有可用信息         | 用户消息、历史对话、环境状态 | 完整上下文              |
| **Reason 推理**   | LLM 分析当前状态         | 完整上下文 + System Prompt   | 下一步行动决策          |
| **Plan 规划**     | 将任务分解为可执行步骤   | 行动决策                     | 工具调用序列            |
| **Act 执行**      | 调用工具/API执行操作     | 工具名称 + 参数              | 工具返回结果            |
| **Observe 观察**  | 收集执行结果，更新上下文 | 工具返回结果                 | 更新的对话历史          |
| **Evaluate 评估** | 判断是否完成或继续       | 更新后的上下文               | Final Answer 或继续循环 |

#### Loop 模式对比

不同的 Agent 框架采用了不同的循环策略：

| 模式             | 核心思路             | 优势               | 劣势                     | 典型框架        |
| :--------------- | :------------------- | :----------------- | :----------------------- | :-------------- |
| **ReAct**        | 推理+行动交替循环    | 灵活、可解释       | 每轮都调LLM，Token消耗大 | LangChain ReAct |
| **ReWOO**        | 规划→执行→求解三阶段 | 减少LLM调用次数    | 规划不可动态调整         | ReWOO 论文      |
| **LLM Compiler** | 并行执行多个工具     | 效率高、延迟低     | 依赖之间不可并行         | LangGraph       |
| **Reflexion**    | 执行后反思，自我纠正 | 自我进化、持续改进 | 反思轮次可能过多         | Reflexion 论文  |



### Runtime——智能体运行时

将 Agent Loop 的**逻辑语义**转化为**可执行计算**的基础设施层。类比编程语言的运行时（JVM 为 Java 提供、V8 为 Node.js 提供）

#### 五层架构

现代 Agent Runtime 通常包含以下五层，从底层到上层依次为：

| 层级           | 职责                                                         | 关键组件                                       | 对应概念           |
| :------------- | :----------------------------------------------------------- | :--------------------------------------------- | :----------------- |
| **LLM 管理层** | 模型路由、Token 管理、重试与降级                             | Model Router、Token Counter、Retry Policy      | Agent 的"大脑供给" |
| **工具注册层** | 工具定义、参数校验、权限控制、调用日志                       | Tool Registry、Validator、Permission Guard     | Agent 的"手脚管控" |
| **记忆管理层** | 短期记忆（上下文窗口）、长期记忆（向量数据库）、工作记忆（跨会话存储） | Context Window、Vector DB、Session Store       | Agent 的"记忆系统" |
| **执行调度层** | Loop 引擎、并发控制、超时与熔断                              | Loop Engine、Concurrency Pool、Circuit Breaker | Agent 的"运动神经" |
| **可观测层**   | Tracing、Metrics、Logging、Debug                             | OpenTelemetry、Dashboard、Debug UI             | Agent 的"体检报告" |

#### 主流框架对比

| 框架                  | 核心思路             | Loop 模式      | 记忆管理           | 工具生态           |
| :-------------------- | :------------------- | :------------- | :----------------- | :----------------- |
| **LangChain**         | 链式组合、模块化     | ReAct + 自定义 | 短期/长期/工作记忆 | 200+内置工具 + MCP |
| **AutoGen**           | 多 Agent 对话协作    | 对话式循环     | 对话历史压缩       | 自定义 + 代码执行  |
| **CrewAI**            | 角色扮演、流程编排   | 顺序/并行/层级 | 短期记忆           | 工具定义装饰器     |
| **OpenAI Agents SDK** | 轻量级、Handoff 交接 | 单 Agent 循环  | 对话历史           | Function Calling   |
| **Google ADK**        | 端到端、多模态       | ReAct + 反思   | 内置记忆模块       | Google 工具生态    |

✅ 选型建议

> 构建企业级 Agent 或多智能体协作应用，推荐使用 Google ADK（Agent Development Kit）。它提供原生的多智能体编排能力、内置丰富的 Google 工具生态（Search、Maps、Vertex AI 等）以及清晰的异步工作流，更适合工程化落地。需要多 Agent 协作也可考虑 AutoGen 或 CrewAI；追求轻量可选 OpenAI Agents SDK；常规场景 LangChain 生态也能覆盖。

### Sandbox——安全执行沙箱

##### 隔离技术全景

| 隔离级别        | 技术方案                                | 隔离强度 | 启动速度       | 适用场景                   |
| :-------------- | :-------------------------------------- | :------- | :------------- | :------------------------- |
| **进程级**      | subprocess + 资源限制（ulimit/cgroups） | 🟡 低     | ⚡ 毫秒         | 简单代码执行、快速验证     |
| **容器级**      | Docker / gVisor / Podman                | 🟢 中     | ⚡ 秒级         | Web 应用、API 服务、多租户 |
| **微虚拟机**    | Firecracker / Kata Containers           | 🔴 高     | ⏱ 125ms~秒级   | 金融/医疗等高安全场景      |
| **WebAssembly** | WASM Runtime（Wasmtime/Wasmer）         | 🟢 中     | ⚡ 毫秒         | 浏览器端执行、轻量计算     |
| **云沙箱**      | E2B / Modal / Daytona / CubeSandbox     | 🟢 中高   | ⚡ 秒级（托管） | 不想自建沙箱的开发者       |

🛡️ 黄金法则：最小权限原则

Agent Sandbox 应遵循**默认拒绝一切，按需开白**原则。

绝对不能暴露给 Sandbox 的资源：宿主机 Docker Socket、云厂商元数据 API、内网服务发现端口、SSH 私钥目录。

#### 工业级沙箱实践：Claude Code 案例

 第一层：命令安全检查（应用级静态分析）：**应用级静态分析**——相当于在门口过安检。这层完全在 JS/TS 代码中完成

 第二层：权限决策引擎（沙箱入口）：**权限决策引擎**——决定这条命令是在沙箱内自动执行、需要用户确认、还是直接拒绝。核心是 `shouldUseSandbox()` 函数的三步决策

```
Step 1：沙箱是否启用？
│
├─ 否
│   └─→ 降级为 ask 模式
│       └─ 要求用户手动确认
│
└─ 是
    ↓
Step 2：是否显式禁用沙箱？
│   - dangerouslyDisableSandbox
│   - 受 allowUnsandboxedCommands 限制
│
├─ 是（且允许禁用）
│   └─→ 进入后续执行流程
│
└─ 否
    ↓
Step 3：是否在排除列表？
    - 匹配 excludedCommands
    - 用户便利功能，非安全边界
    - 支持复合命令拆分匹配
    ↓
进入沙箱执行
├─ auto-allow：自动执行
└─ regular：需用户确认
```

三种沙箱模式：

| 模式           | 行为                         | 适用场景                 |
| :------------- | :--------------------------- | :----------------------- |
| **auto-allow** | 沙箱内命令自动执行，无需确认 | 可信项目、日常开发       |
| **regular**    | 每条命令需用户确认后执行     | 新项目、敏感环境         |
| **disabled**   | 不使用沙箱，所有命令需确认   | 沙箱不可用或用户显式关闭 |



第三层：OS 沙箱执行（系统级隔离）:通过权限决策后，命令最终在**操作系统级沙箱**中执行。Claude Code 根据平台选择不同的 OS 沙箱技术：

| 平台           | 沙箱技术                 | 原理                                                         | 依赖                              |
| :------------- | :----------------------- | :----------------------------------------------------------- | :-------------------------------- |
| **macOS**      | Seatbelt（sandbox-exec） | 内核级沙箱配置文件，限制文件系统访问、网络、系统调用         | 系统内置，零安装                  |
| **Linux/WSL2** | Bubblewrap + seccomp     | bwrap 创建挂载命名空间（文件系统隔离），seccomp 过滤系统调用 | 需 `apt install bubblewrap socat` |
| **Windows**    | PowerShell 沙箱包装      | `pwsh -NoProfile -NonInteractive -EncodedCommand` 预包装     | 系统内置                          |

📁 文件系统三级控制

Bubblewrap 通过挂载命名空间实现三级文件系统控制：

- **可写**（`--bind`）：项目目录、`/tmp`——Agent 可读写
- **只读**（`--ro-bind`）：系统库、`/usr/bin`——Agent 可读不可写
- **屏蔽**（`--dev-null`）：敏感路径——Agent 完全不可见

特别地，Claude Code **禁止写入** `settings.json` 和 `.claude/skills/` 目录——防止 Agent 修改自身配置实现沙箱逃逸。

🌐 网络隔离架构

沙箱网络通过**代理 + 域名白名单**实现，而非直接禁用网络：

- **HTTP 代理**（`httpProxyPort`）：MITM 代理拦截 HTTP 请求，只允许 `allowedDomains` 中的域名
- **SOCKS 代理**（`socksProxyPort`）：拦截 TCP 连接，非白名单域名直接拒绝
- **Unix Domain Sockets**：通过 seccomp 阻断（防 `/var/run/docker.sock` 逃逸）

这意味着 Agent 可以 `npm install`（npm registry 在白名单中），但不能 `curl http://evil.com`。



🚨 裸 Git 仓库攻击防护

攻击者可创建一个裸 Git 仓库，设置 `core.fsmonitor` 指向恶意脚本。当 Agent 在该仓库中执行 `git status` 时，Git 会自动执行 `core.fsmonitor` 指定的脚本——绕过沙箱的命令检查。

Claude Code 的防御：`scrubBareGitRepoFiles()` 在进入沙箱前扫描并清除裸 Git 仓库的危险配置文件。这是**针对真实攻击向量的深度防御**。

####  三层防御的纵深协同

```
                         ┌──────────────────────┐
                         │   LLM 生成 Bash 命令  │
                         └──────────┬───────────┘
                                    │
                                    ▼
                  ┌─────────────────────────────────┐
                  │      第一层：命令安全检查       │
                  │      23+ 验证器 + AST 解析      │
                  │   检测注入、混淆、危险路径      │
                  └──────────────┬──────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
                   通过                    检测到威胁
                    │                         │
                    ▼                         ▼
        ┌───────────────────────────┐    ┌────────────────────┐
        │    第二层：权限决策       │    │ 阻断：返回安全告警 │
        │  shouldUseSandbox()       │    └────────────────────┘
        │  三步决策                 │
        │  auto-allow / regular /   │
        │  disabled                 │
        └─────────────┬─────────────┘
                      │
              ┌───────┴───────────────┐
              │                       │
           进入沙箱                需用户确认
              │                       │
              ▼                       ▼
┌───────────────────────────────┐   ┌──────────────────────┐
│      第三层：OS 沙箱执行      │   │ 降级：要求用户确认   │
│ Seatbelt / Bubblewrap         │   └──────────────────────┘
│ + seccomp                     │
│ 文件系统隔离 + 网络代理       │
└──────────────┬────────────────┘
               │
       ┌───────┴───────────────┐
       │                       │
    执行成功                 违规操作
       │                       │
       ▼                       ▼
┌─────────────────────────┐   ┌──────────────────────────┐
│ 命令在隔离环境中执行    │   │ 拒绝：违规记录到        │
│ 资源受限、网络过滤、    │   │ ViolationStore          │
│ 路径隔离                │   └──────────────────────────┘
└─────────────────────────┘
```





### 三者协同：完整协作流程

在真实的 Agent 系统中，Loop、Runtime 和 Sandbox 是紧密耦合的：Loop 调度 Runtime，Runtime 委托 Sandbox 执行不可信代码。

```python
class SecureAgent:
    """完整集成：AgentLoop + Runtime（ToolRegistry + Memory） + SandboxExecutor"""

    def __init__(self, llm, sandbox: SandboxExecutor):
        self.loop = AgentLoop(llm, tools=[], memory=InMemoryMemory(), max_turns=10)
        self.registry = ToolRegistry()
        self.sandbox = sandbox

        # 注册工具：代码生成（在 Sandbox 中执行）
        self.registry.register(
            name="execute_code",
            description="在安全沙箱中执行Python代码",
            parameters={"code": {"type": "string", "required": True, "description": "Python代码"}},
            handler=lambda args: self.sandbox.execute_in_sandbox(
                "code_execution", "execute_code", args["code"]),
            allowed_roles=["assistant"],
        )

        # 注册工具：搜索
        self.registry.register(
            name="web_search",
            description="搜索网页内容",
            parameters={"query": {"type": "string", "required": True}},
            handler=lambda args: search_web(args["query"]),
            allowed_roles=["*"],
        )

        self.loop.tools = self.registry.tools

    def run(self, user_input: str) -> str:
        """运行完整的 Agent 系统"""
        return self.loop.run(user_input)
```

### Agent Loop 稳定性设计

#### 心跳检测与空闲超时

Agent 在执行长任务时,用户可能已经离开。为避免浪费 Token 和算力,Loop 应实现**心跳检测**:

JavaScript📋

```javascript
// 空闲超时配置
const IDLE_TIMEOUT_MS = 10 * 60 * 1000; // 10 分钟

// Loop 中心跳检测
let lastActivityTime = Date.now();

function checkHeartbeat() {
  const idle = Date.now() - lastActivityTime;
  if (idle > IDLE_TIMEOUT_MS) {
    return {
      action: 'pause',
      reason: `空闲超过 ${IDLE_TIMEOUT_MS / 60000} 分钟,自动暂停`,
      canResume: true
    };
  }
  return { action: 'continue' };
}

// 用户交互时重置计时器
function onUserActivity() {
  lastActivityTime = Date.now();
}
```

### 

| 保障机制            | 解决问题                      | 关键参数                            | 章节参考         |
| :------------------ | :---------------------------- | :---------------------------------- | :--------------- |
| **上下文溢出熔断**  | Context 无限增长导致 API 报错 | max_context_tokens = 120K           | ch05 5.12 + 本节 |
| **死循环检测**      | 工具反复失败导致 Loop 卡死    | max_turns = 20, max_tool_errors = 3 | ch21 7.2.4       |
| **压缩降级链**      | 压缩失败导致历史丢失          | AI→规则→硬截断                      | 第7章 6.12.7     |
| **Checkpoint 恢复** | 中断后无法继续任务            | 状态序列化 + 反序列化               | 本节             |
| **心跳检测**        | 用户离开后 Agent 继续烧钱     | idle_timeout = 10min                | 本节 8.6.3       |





## 🔑 八股卡

> 教程自带的「📋 八股总结」「📋 补充八股题」原文，**整段贴进来**。
> **想加强的那几条，用 `==……==` 把标题包起来。**

Q3: Docker 容器和 Firecracker 微虚拟机做 Sandbox 有什么区别？

**Docker** 共享宿主机内核——隔离在进程/文件系统层面，内核漏洞可逃逸。
**Firecracker** 每个沙箱运行独立内核——内核级隔离，逃逸难度极高。

代价对比：Docker 启动秒级、资源开销小；Firecracker 启动约125ms、资源开销更大。

**选型原则**：金融/医疗等高安全场景选 Firecracker，一般 Web 场景 Docker 够用，不想自建选 E2B/Modal 等云沙箱服务。

Q5: Loop、Runtime、Sandbox 三者的嵌套关系？各层的职责边界？

**嵌套关系**：Sandbox 在最外层（所有行为在约束下执行），Runtime 在中间层（提供基础设施），Agent Loop 在最内层（决策循环）。

**职责边界**：
① Agent 该做什么决策？→ **Agent Loop**（LLM 推理决定下一步行动）；
② 工具参数是否合法？→ **Runtime 工具注册层**（校验类型、值域、必填项）；
③ 调用是否有权限？→ **Runtime 权限控制层**（角色权限矩阵 + 审批机制）；
④ 代码在哪里执行？→ **Sandbox**（创建隔离环境、限制资源）；
⑤ 历史对话太长怎么办？→ **Runtime 记忆管理层**（滑动窗口压缩 + 摘要替代）

---

## 🧩 面试题（原题 + 我的答案）

> **只记做错的题**，对的不用抄。
> 记法：`~~X~~ Y` = 我选 X（错），正确答案是 Y。



6. Runtime 层的核心职责不包括以下哪项？

A管理 Agent 生命周期（启动、暂停、恢复、终止）

B提供工具执行环境（文件系统、进程、网络）

C执行 LLM 推理计算（前向传播）

D资源配额管理（CPU、内存、时间限制）

**解析：**Runtime 层负责 Agent 生命周期管理、工具执行环境和资源配额，但不执行 LLM 推理计算。LLM 推理由模型服务端（如 OpenAI API、本地 vLLM）完成，Runtime 只负责调用 API 并处理响应。



2. Loop、Runtime、Sandbox 三者的嵌套关系是？

ALoop 在最外层，Runtime 在中间层，Sandbox 在最内层

BRuntime 在最外层，Sandbox 在中间层，Loop 在最内层

CSandbox 在最外层，Runtime 在中间层，Loop 在最内层

D三者并列，无嵌套关系

**解析：**Sandbox 在最外层（所有 Agent 行为最终都在某个隔离环境中执行），Runtime 在中间层（为 Loop 提供基础设施），Agent Loop 在最内层（是 Runtime 中的逻辑循环）。





---

## ❓ 疑问 / 待查

> 汇总本章没解决的疑问（正文里就地标过的，这里可再列一次，方便统一解决）。

- [ ] （疑问）



---

## 🔗 关联

- **前置**：
- **后续**：
# 第 6 章 · ReAct：让 Agent 学会思考

> **来源**：<https://ai-agent-guide.xiaofuge.cn/> → 第 6 章
> **记录日期**：2026-09-16（09-17 扩写）　|　**重要度**：★★★　|　**掌握度**：

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



### 用 50 行代码实现 ReAct

```python
import re
from openai import OpenAI

client = OpenAI()
TOOLS = {"search": search_web, "lookup": lookup_keyword}

def react_agent(question, max_steps=10):
    messages = [{"role": "system", "content": REACT_PROMPT}]
    messages.append({"role": "user", "content": question})
    
    for step in range(max_steps):
        # 1. LLM 生成 Thought + Action
        response = client.chat.completions.create(
            model="gpt-4o", messages=messages
        )
        output = response.choices[0].message.content
        messages.append({"role": "assistant", "content": output})
        
        # 2. 解析 Action
        action_match = re.search(r'Action: (\w+)\((.*?)\)', output)
        if not action_match:
            break
            
        tool_name = action_match.group(1)
        tool_input = action_match.group(2).strip('"\'')
        
        # 3. 检查是否完成
        if tool_name == "finish":
            return tool_input
        
        # 4. 执行工具
        if tool_name in TOOLS:
            result = TOOLS[tool_name](tool_input)
        else:
            result = f"Error: tool {tool_name} not found"
        
        # 5. 注入 Observation
        messages.append({"role": "user", "content": f"Observation: {result}"})
    
    return "达到最大步数限制"

# 使用
answer = react_agent("2024年诺贝尔物理学奖得主是谁？")
```

核心就这几步：**LLM 生成 → 解析 Action → 执行工具 → 注入 Observation → 循环**。

### ReWOO 模式（Reasoning Without Observation）

ReAct 的核心循环是"边想边做"——每一步 Thought 之后立刻 Action，获得 Observation 后再想下一步。这很直观，但有一个严重的问题：**每一步的 Observation 都会被塞入上下文，随着步数增加，token 消耗线性膨胀。**

ReWOO 提出了一个反直觉的思路：**先想好所有步骤，再统一执行。**

### ReWOO 代码示例

```python
from openai import OpenAI

client = OpenAI()
TOOLS = {"search": search_web, "weather": get_weather, "calc": calculate}

# ===== Phase 1: Planner =====
PLANNER_PROMPT = """你是一个规划器。根据用户问题，生成一系列步骤来解决问题。
每一步用 #E 标记需要执行的工具调用。

格式：
Plan: 第一步的推理
#E1 = tool_name("参数")
Plan: 第二步的推理（可引用 #E1 的结果）
#E2 = tool_name("参数")
...

问题: {question}"""

def plan_steps(question):
    """Phase 1: LLM 一次性规划所有步骤"""
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": PLANNER_PROMPT},
                  {"role": "user", "content": question}]
    )
    return resp.choices[0].message.content

# ===== Phase 2: Worker =====
def execute_steps(plan_text):
    """Phase 2: 解析并执行所有 #E 工具调用"""
    import re
    results = {}
    # 查找所有 #E 标记
    steps = re.findall(r'#E(\d+)\s*=\s*(\w+)\((".*?"|\'.*?\'|.*?)\)', plan_text)
    for step_id, tool_name, tool_input in steps:
        tool_input = tool_input.strip('"\'')
        if tool_name in TOOLS:
            results[f"#E{step_id}"] = TOOLS[tool_name](tool_input)
        else:
            results[f"#E{step_id}"] = f"Error: {tool_name} not found"
    return results

# ===== Phase 3: Solver =====
SOLVER_PROMPT = """根据以下规划和执行结果，给出最终答案。

规划:
{plan}

执行结果:
{results}

请综合以上信息，给出完整答案。"""

def solve_answer(plan_text, results):
    """Phase 3: LLM 综合所有结果生成答案"""
    # 将结果填入规划的 #E 占位符
    filled_plan = plan_text
    for key, value in results.items():
        filled_plan = filled_plan.replace(key, f"[结果: {value}]")
    
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": SOLVER_PROMPT},
                  {"role": "user", "content": 
                   f"规划:\n{plan_text}\n\n执行结果:\n{filled_plan}"}]
    )
    return resp.choices[0].message.content

# ===== 完整流程 =====
def rewoo_agent(question):
    """ReWOO: Planner → Worker → Solver"""
    # 1. 规划
    plan = plan_steps(question)
    # 2. 执行
    results = execute_steps(plan)
    # 3. 求解
    answer = solve_answer(plan, results)
    return answer

# 使用
answer = rewoo_agent("北京今天天气如何？适合户外活动吗？")
```

#### **LLM Compiler**

*2023 年由 Kim 等人提出。核心思想：将 Agent 的执行计划编译成***依赖图（DAG）***，识别可并行的步骤，同时执行没有依赖关系的工具调用，显著减少总执行时间。*

#### **Reflexion = ReAct + Self-Reflection**

*2023 年由 Shinn 等人提出。核心思想：Agent 执行完任务后，***回头审视自己的推理过程***，评估结果质量，发现问题则重新尝试。通过多轮"执行→反思→改进"循环，逐步提升答案质量。*

### 🔄 ReAct 家族演进

| 变体                   | 改进点                               | 核心机制                | 年份  |
| :--------------------- | :----------------------------------- | :---------------------- | :---- |
| ReAct                  | 原始版：Thought-Action-Observation   | 推理+行动交替循环       | 2022  |
| ReWOO                  | 先规划再执行，减少token消耗          | Planner→Worker→Solver   | 2023  |
| LLM Compiler           | 并行执行独立工具调用                 | 依赖图(DAG)编译+并行    | 2023  |
| Reflexion              | 加入自我反思，从失败中学习           | 执行→评估→反思→重试循环 | 2023  |
| LATS                   | 结合蒙特卡洛树搜索+ReAct             | 树搜索+推理行动         | 2023  |
| Function Calling ReAct | 用原生 Function Calling 替代文本解析 | 结构化工具调用          | 2023+ |

### 生产级 Agent 主循环设计

**双模式**：Chat 简单高效，Agent 自动多轮，按场景选择避免浪费。

**Token Budget**：全局预算防止烧钱，实时累加及时停止。

**死循环保护**：连续 2 轮相同失败自动终止，阈值 2 平衡了灵敏度和容错。

**截断恢复**：max_tokens 截断时注入 continue 自动续写，有限次恢复避免无限循环。

**指数退避**：临时错误自动重试，2^n 秒退避避免雪崩，区分可重试和不可重试错误。

**413 压缩**：请求过大时紧急压缩重试，是正常压缩的兜底方案。



## 🔑 八股卡

> 教程自带的「📋 八股总结」「📋 补充八股题」原文，**整段贴进来**。
> **想加强的那几条，用 `==……==` 把标题包起来。**

Q4: ReAct 模式有哪些局限性？后续如何改进？

**局限**：① Token 消耗线性增长（每步带完整历史）；② 严格串行无法并行；③ 格式解析不稳定；④ 可能陷入死循环。

**改进方向**：① ReWOO 先规划后执行减少token；② LLM Compiler 并行执行独立步骤；③ Reflexion 加入自我反思从失败学习；④ Structured Output 解决格式不稳定；⑤ 上下文压缩控制 token 消耗。



Q10: 标准 ReAct 模式在生产环境中有哪些局限？如何用 Fork 子代理机制增强？

**局限**：①标准 ReAct 串行执行，独立子任务无法并行；②自我确认偏误——Agent 用自己的标准检查自己的代码，容易忽略错误；③自由推理容易导致任务遗漏和优先级漂移。

**Fork 子代理增强**：主代理在 Thought 阶段判断子任务可委派，Fork 子代理后台执行。子代理继承主代理上下文，共享 prompt cache。完成后发通知，主代理整合结果。

**Fork Prompt 四规则**：不偷看（通知到达前不访问 output_file）、不编造（不能猜结果）、不推诿理解（不能把综合判断推给子代理）、指令式而非背景介绍式（Fork 已继承上下文，不需要重复解释）。

---

## 🧩 面试题（原题 + 我的答案）

> **只记做错的题**，对的不用抄。
> 记法：`~~X~~ Y` = 我选 X（错），正确答案是 Y。



12. 关于 Structured Output，以下说法正确的是？（多选）

AJSON Mode 只保证输出是合法 JSON，不保证字段类型和必填项

BFunction Calling 的参数有 Schema 约束，但保证程度中等

CStructured Output 通过完整 JSON Schema 约束，100% 保证格式

DStructured Output 可以完全替代 Reflexion 的反思能力

**解析：**Structured Output 解决的是输出格式可靠性问题，与 Reflexion 的反思推理能力是不同维度的问题，不能替代。



10. 关于 ReAct 的局限性和改进，以下说法正确的是？（多选）

AToken 消耗随步数线性增长是主要局限之一

BReflexion 在 ReAct 基础上加入了自我反思能力

CFunction Calling 可以替代文本解析提升格式稳定性

DReAct 可以自然支持多 Agent 协作

**解析：**ReAct 本身是单 Agent 模式，不直接支持多 Agent 协作（需要额外框架如 CrewAI）。其他三项都正确。

---

## ❓ 疑问 / 待查

> 汇总本章没解决的疑问（正文里就地标过的，这里可再列一次，方便统一解决）。

- [ ] （疑问）

---

## 🔗 关联

- **前置**：
- **后续**：
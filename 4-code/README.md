# 4-code · 配套代码

教程配套的实操代码。**这一层是容器**：里面两个 demo 互相独立，各有各的跑法，不共享代码。

| 目录 | 是什么 | 跑起来 |
|---|---|---|
| `first_agent/` | 教学用**最小 Agent**：Agent 循环 + 调用链 Tracer + 两个工具（`get_weather` / `plan_trip`） | 在**仓库根目录**执行 `uv run python 4-code/first_agent/agent.py` |
| `maodingcode/` | **MaoDingCode**：完整 Agent 运行时（Loop / 上下文工程 / 权限沙箱 / MCP / Skills / 可观测性）。纯标准库、**零第三方依赖**，配 13 篇设计文档 + 38 项离线测试 | `cd 4-code/maodingcode && python run_demo.py`（不用 API Key） |

两者依赖不同，跑法因此也不同：

- `first_agent/` 用 `openai` + `python-dotenv`，这两个依赖由**仓库根的 uv 工程**提供（根 `pyproject.toml` / `.venv`），所以要用 `uv run` 从仓库根启动。
- `maodingcode/` 零第三方依赖，任何 Python 3.11+ 直接跑即可，不需要 uv。

## 环境变量

`.env.example` 放在本层，两个 demo 共用；复制成 `.env` 后也放**本层**（`4-code/.env`，已 gitignore）：

```bash
cp 4-code/.env.example 4-code/.env     # 然后填 AI_API_KEY
```

`maodingcode` 读的是「工作区根」的那份 `.env`，而它的工作区默认就是本层，位置刚好一致。

## 两个容易踩的点

**1. `python -m maodingcode` 必须在本层执行，不能在 `maodingcode/` 里跑**

项目根与包目录被拍平成同一层，导入根因此上移到了本层：

```bash
cd 4-code                     # 本层 = 导入根
python -m maodingcode --help
```

在 `maodingcode/` 目录里执行会报 `No module named maodingcode`。
反过来，`python run_demo.py` / `python run_tests.py` 是在 `maodingcode/` 目录里跑的。

**2. 为什么不直接写成 `4-code/MaoDingCode/maodingcode/`**

Windows 文件系统大小写不敏感，外层目录与内层包名会被判定为同一个目录，那个布局建不出来 ——
所以把项目根和包目录合并成一层。起因、取舍与目录全貌写在 [`maodingcode/README.md`](maodingcode/README.md)。

## 各 demo 的文档

- **`first_agent/`**
  - `TODO` —— 待优化清单（上下文控制等）
  - 对应知识库笔记：[13-Agent-Loop与多轮对话](../1-知识库/13-Agent-Loop与多轮对话.md) · [14-工具Schema与异常处理](../1-知识库/14-工具Schema与异常处理.md) · [15-多工具协作与调用链成本](../1-知识库/15-多工具协作与调用链成本.md) · [16-Agent边界与混合架构](../1-知识库/16-Agent边界与混合架构.md) · [19-ReAct循环与推理范式家族](../1-知识库/19-ReAct循环与推理范式家族.md)
- **`maodingcode/`**
  - `README.md` —— 上手与目录规则
  - `docs/` —— 13 篇设计文档；其中 `11-实现踩坑记录.md`（开发中真实踩到的 bug）与 `12-面试讲述手册.md`（怎么把这套东西讲出去）值得单独看
  - `vscode-extension/README.md` —— 编辑器插件

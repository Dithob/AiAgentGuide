"""零配置演示 —— 不需要 API Key、不联网、不花钱。

    python run_demo.py

它做了一件挺有意思的事：**在本地起一个假模型服务**（真的 HTTP + 真的 SSE），
把 Agent 指向它，然后跑一个完整的多步任务。所以你看到的是**真实链路**：

    终端 ─▶ AgentLoop ─▶ Provider（真 HTTP）─▶ 本地假模型
                 │                                  │
                 └────── 真工具执行（真的写文件）◀────┘

而不是"打印一段假日志"。想验证产品能不能跑，这个 demo 就够了。

想接真模型：把 AI_API_KEY / AI_BASE_URL 配好，直接跑
    python -m maodingcode
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 项目目录 maodingcode/（tests/ 在它下面）
IMPORT_ROOT = HERE.parent                        # maodingcode 包的父目录（即 4-code/）
for _p in (str(IMPORT_ROOT), str(HERE / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fake_llm import FakeLLM, text_response, tool_call_response  # noqa: E402
from maodingcode import __version__  # noqa: E402
from maodingcode.config import load_config  # noqa: E402
from maodingcode.core.loop import LoopEvent  # noqa: E402
from maodingcode.session import Session  # noqa: E402
from maodingcode.ui import render  # noqa: E402


# --------------------------------------------------------------------- 剧本
def build_script() -> list[dict]:
    """模拟一个真实的多步任务：先看、再写、再验证、最后汇报。

    注意这些都是标准的 OpenAI 响应格式，假模型只是把它们按顺序发出来。
    """
    return [
        # 第 1 步：模型先看目录
        tool_call_response("list_dir", {"path": "."}, call_id="c1"),
        # 第 2 步：写文件
        tool_call_response(
            "write_file",
            {
                "path": "maoding_demo/hello.py",
                "content": (
                    '"""由 MaoDingCode 生成的示例文件。"""\n\n\n'
                    "def hello(name: str) -> str:\n"
                    '    return f"你好，{name}！"\n\n\n'
                    'if __name__ == "__main__":\n'
                    '    print(hello("世界"))\n'
                ),
            },
            call_id="c2",
        ),
        # 第 3 步：跑一下验证（suggest 模式下会被拦下来问确认）
        tool_call_response(
            "run_shell",
            {"command": f'{sys.executable} -c "import sys; print(1+1)"', "timeout": 20},
            call_id="c3",
        ),
        # 第 4 步：汇报
        text_response(
            "已经完成：\n\n"
            "1. 看了工作区结构；\n"
            "2. 新建 `maoding_demo/hello.py`，里面是一个带中文输出的 hello 函数；\n"
            "3. 运行验证通过。\n\n"
            "你可以直接 `python maoding_demo/hello.py` 看到效果。"
        ),
    ]


# --------------------------------------------------------------------- 渲染
class DemoRenderer:
    """把 Loop 事件渲染成终端输出。和真 CLI 用的是同一套 render。"""

    def __init__(self, auto_allow: bool = True) -> None:
        self.auto_allow = auto_allow
        self.streamed = False

    def on_event(self, event: LoopEvent) -> None:
        kind, payload = event.kind, event.payload

        if kind == "delta":
            sys.stdout.write(str(payload.get("text") or ""))
            sys.stdout.flush()
            self.streamed = True
            return
        if kind == "assistant_message":
            return
        if kind == "tool_result":
            print(render.render_event(kind, payload) or "")
            if payload.get("ok") and payload.get("output"):
                print(render.tool_output_preview(str(payload["output"]), limit=6))
            elif not payload.get("ok"):
                print(render.tool_output_preview(str(payload.get("output", ""))))
            return
        text = render.render_event(kind, payload)
        if text:
            print(text)

    def on_confirm(self, tool_name: str, args: dict, reason: str) -> bool:
        """演示里自动同意，但把"这里本来要问用户"这件事显式打出来。"""
        print(render.confirm_prompt(tool_name, args, reason))
        print(render.hint(f"（演示模式：自动回答 y；真实运行时会在这里等你输入）"))
        if self.auto_allow:
            return True
        return False


# --------------------------------------------------------------------- 主流程
def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="maoding-demo-"))
    # 放两个文件进去，让 list_dir 的输出看起来不像空目录
    (workspace / "README.md").write_text("# 演示工作区\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    print(render.bold(render.cyan(f"MaoDingCode v{__version__}")) + render.dim("  零配置演示"))
    print(render.dim("─" * 68))
    print(render.hint("本演示会启动一个本地假模型服务（真 HTTP + 真 SSE），"))
    print(render.hint("所以整条链路都是真的，只有「大脑」是写死的剧本。"))
    print(render.hint(f"演示工作区：{workspace}"))
    print(render.dim("─" * 68))

    with FakeLLM(build_script()) as llm:
        # 用假模型的地址组装一份配置。permission_mode=suggest 才能演示权限确认。
        cfg = load_config(
            workspace=workspace,
            overrides={
                "workspace": str(workspace),
                "api_key": "demo-key-not-a-real-key",
                "base_url": llm.base_url,
                "model": "demo-model",
                "permission_mode": "suggest",
                "max_steps": 8,
                "stream": True,
            },
        )
        session = Session(cfg)

        print()
        print(render.bold("› ") + "帮我在这个项目里加一个 hello 模块，并验证它能跑")
        print(render.dim("─" * 68))

        renderer = DemoRenderer()
        loop = session.make_loop(emit=renderer.on_event, confirm=renderer.on_confirm)
        result, summary = session.run_turn(
            "帮我在这个项目里加一个 hello 模块，并验证它能跑",
            emit=renderer.on_event,
            loop=loop,
        )

        print()
        if result.content:
            if renderer.streamed:
                print()
            else:
                print(result.content)

        print(render.cost_report(summary))
        session.close()

        # 验证产物确实落盘了
        produced = workspace / "maoding_demo" / "hello.py"
        print()
        if produced.is_file():
            print(render.green("✓") + f" 产物已生成：{produced}")
            print(render.dim("─" * 68))
            print(produced.read_text(encoding="utf-8"))
        else:
            print(render.red("✗") + " 没有生成预期文件（这不该发生）")
            return 1

        if llm.remaining:
            print(render.warn(f"剧本还剩 {llm.remaining} 条未使用，说明循环提前结束了"))
            return 1

    print(render.dim("─" * 68))
    print(render.hint("演示结束。接真模型：配置 AI_API_KEY / AI_BASE_URL 后跑 `python -m maodingcode`"))
    print(render.hint("编辑器集成：`python -m maodingcode --rpc`，或安装 vscode-extension/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

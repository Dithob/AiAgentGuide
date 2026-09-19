"""RPC 模式的端到端测试。

这里**真的启动一个子进程**，用真的 HTTP 打真的假模型服务：

    test ──spawn──▶ python -m maodingcode --rpc ──HTTP/SSE──▶ FakeLLM
      ▲                     │
      └──── NDJSON ─────────┘

比在进程内直接调函数强的地方：验证了 stdio 管道、线程模型、
权限确认的跨线程同步、abort 语义这些只有真的两种方式跑起来才暴露的问题。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# IMPORT_ROOT = maodingcode 包的父目录（即 4-code/）。
# 它有两个用途：import maodingcode、以及 spawn 子进程跑 `python -m maodingcode` 时的 cwd。
# 项目根与包目录已合并成同一层，所以这里必须上溯两层（parents[2]）而不是 parent.parent。
IMPORT_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for _p in (str(IMPORT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fake_llm import (  # noqa: E402
    FakeLLM,
    text_response,
    tool_call_response,
)

STARTUP_TIMEOUT = 30.0
EVENT_TIMEOUT = 40.0


def _pass(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise AssertionError(msg)


class RpcProcess:
    """驱动一个真实的 RPC 子进程。"""

    def __init__(self, workspace: Path, base_url: str, *, mode: str = "auto", stream: bool = True) -> None:
        env = dict(os.environ)
        # 用干净且确定的配置，避免本机 .env / 环境变量干扰测试
        for key in list(env):
            if key.startswith(("AI_", "MOD_")):
                env.pop(key, None)
        env.update(
            {
                "AI_PROVIDER": "fake",
                "AI_BASE_URL": base_url,
                "AI_API_KEY": "fake-key",
                "AI_MODEL_NAME": "fake-model",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )

        cmd = [
            sys.executable,
            "-m",
            "maodingcode",
            "--rpc",
            "-C",
            str(workspace),
            "--mode",
            mode,
        ]
        if not stream:
            cmd.append("--no-stream")

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(IMPORT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.events: "queue.Queue[dict]" = queue.Queue()
        # 所有读到的事件都留一份副本并保持顺序。
        # 断言必须基于它，而不是基于"读完 done 之后还能捞到什么"——
        # delta 是在 done 之前到达的，用完即弃的读法永远看不到它们。
        self.seen: list[dict] = []
        self._stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()

    # ------------------------------------------------------------------ IO
    def _read_stdout(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"type": "__bad_json__", "raw": line}
            self.events.put(event)

    def _read_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            self._stderr_lines.append(line.rstrip())

    def send(self, **payload: Any) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def next_event(self, timeout: float = EVENT_TIMEOUT) -> dict:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"等待事件超时（{timeout}s）。已收到的 stderr：\n"
                + "\n".join(self._stderr_lines[-20:])
            ) from None
        self.seen.append(event)
        return event

    def wait_for(self, type_: str, timeout: float = EVENT_TIMEOUT) -> dict:
        """等到某个类型的事件。途中的事件一并计入 self.seen，不会丢。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.next_event(timeout=max(0.5, deadline - time.time()))
            if event.get("type") == type_:
                return event
            if event.get("type") == "fatal":
                raise AssertionError(f"进程启动失败：{event.get('message')}")
        raise AssertionError(
            f"没等到 {type_}。已收到的类型序列：{self.types()}\n"
            f"stderr：{self._stderr_lines[-20:]}"
        )

    def drain(self, seconds: float = 0.4) -> list[dict]:
        """把还在管道里的事件读干净，用于收尾断言。"""
        deadline = time.time() + seconds
        out: list[dict] = []
        while time.time() < deadline:
            try:
                out.append(self.next_event(timeout=0.1))
            except AssertionError:
                continue
        return out

    def types(self) -> list[str]:
        """已收到的事件类型序列，按到达顺序。"""
        return [e.get("type", "?") for e in self.seen]

    def events_of(self, type_: str) -> list[dict]:
        return [e for e in self.seen if e.get("type") == type_]

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except OSError:
            pass

    def __enter__(self) -> "RpcProcess":
        self.wait_for("ready", timeout=STARTUP_TIMEOUT)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------- 用例
def test_rpc_streaming_full_flow(tmp: Path) -> None:
    print("test_rpc_streaming_full_flow")
    script = [
        tool_call_response(
            "write_file",
            {"path": "out/报告.md", "content": "# 报告\n\n这是中文内容。\n"},
        ),
        text_response("已完成：写入了 out/报告.md"),
    ]
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="auto", stream=True) as rpc:
        rpc.send(cmd="run", prompt="帮我写一份报告")
        done = rpc.wait_for("done")

        if done.get("reason") != "completed":
            _fail(f"应当正常收敛，实际 {done.get('reason')}: {done.get('content')}")
        _pass(f"任务收敛：{done.get('reason')}，{done.get('steps')} 步")

        target = tmp / "out" / "报告.md"
        if not target.is_file():
            _fail("文件没有被真正写入")
        if "中文内容" not in target.read_text(encoding="utf-8"):
            _fail("写入内容不正确（编码问题？）")
        _pass(f"文件已写入：{target.relative_to(tmp)}")

        if done.get("tool_calls") != 1:
            _fail(f"工具调用计数错误：{done.get('tool_calls')}")
        _pass("工具调用统计正确")

        cost = done.get("cost") or {}
        if not cost.get("total_tokens"):
            _fail(f"缺少成本统计：{cost}")
        _pass(f"成本统计：{cost['total_tokens']} tokens / {cost['steps']} 步")

        # 流式：delta 在 done 之前到达，必须能从累积序列里取到
        rpc.drain(0.3)
        deltas = rpc.events_of("delta")
        if not deltas:
            _fail(f"没有收到任何 delta 事件，实际事件序列：{rpc.types()}")
        streamed = "".join(str(d.get("text") or "") for d in deltas)
        if streamed != done.get("content"):
            _fail(f"增量拼接结果与最终回答不一致：{streamed!r} != {done.get('content')!r}")
        _pass(f"流式增量 {len(deltas)} 片，拼接后与最终回答逐字一致")


def test_rpc_event_order_and_streaming(tmp: Path) -> None:
    print("test_rpc_event_order_and_streaming")
    script = [
        tool_call_response("list_dir", {"path": "."}),
        text_response("目录里目前是空的，没有看到任何文件。"),
    ]
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="auto", stream=True) as rpc:
        rpc.send(cmd="run", prompt="看看目录")
        rpc.wait_for("done")
        rpc.drain(0.3)
        kinds = rpc.types()

        for expected in ("step_start", "model_response", "tool_start", "tool_result"):
            if expected not in kinds:
                _fail(f"缺少事件 {expected}，实际序列：{kinds}")
        _pass("事件序列完整（step_start → model_response → tool_start → tool_result → delta）")

        # 顺序：tool_start 必须在 tool_result 之前
        if kinds.index("tool_start") > kinds.index("tool_result"):
            _fail(f"tool_start / tool_result 顺序颠倒：{kinds}")
        _pass("tool_start 早于 tool_result")

        # 流式下正文应当是分片到达的
        deltas = rpc.events_of("delta")
        if len(deltas) < 2:
            _fail(f"流式增量过少，可能没有真的走 SSE：{kinds}")
        _pass(f"正文分 {len(deltas)} 个增量到达（确认走的是 SSE）")


def test_rpc_permission_denied(tmp: Path) -> None:
    print("test_rpc_permission_denied")
    script = [
        tool_call_response("run_shell", {"command": "echo hello"}),
        text_response("好的，我不执行。"),
    ]
    # suggest 模式下 run_shell 属于不可逆操作，必须询问
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="suggest") as rpc:
        rpc.send(cmd="run", prompt="跑个命令")
        ask = rpc.wait_for("needs_confirm", timeout=30)

        if ask.get("tool") != "run_shell":
            _fail(f"确认请求的工具不对：{ask.get('tool')}")
        if "command" not in (ask.get("args") or {}):
            _fail(f"确认请求没带上命令原文：{ask.get('args')}")
        _pass(f"收到确认请求：{ask['tool']}，原因「{ask.get('reason')}」")

        rpc.send(cmd="confirm_response", id=ask["id"], allow=False)
        done = rpc.wait_for("done")

        if done.get("reason") != "completed":
            _fail(f"拒绝后应当继续（而不是崩），实际 {done.get('reason')}")
        _pass("拒绝后被拒绝的结果回灌，循环继续并正常收敛")


def test_rpc_permission_allowed(tmp: Path) -> None:
    print("test_rpc_permission_allowed")
    script = [
        tool_call_response("run_shell", {"command": "echo maoding-ok"}),
        text_response("命令已执行。"),
    ]
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="suggest") as rpc:
        rpc.send(cmd="run", prompt="跑个命令")
        ask = rpc.wait_for("needs_confirm", timeout=30)
        rpc.send(cmd="confirm_response", id=ask["id"], allow=True)
        rpc.wait_for("done")
        rpc.drain(0.3)

        results = rpc.events_of("tool_result")
        if not results:
            _fail("没有收到 tool_result")
        if not results[0].get("ok"):
            _fail(f"已允许的命令却执行失败：{results[0].get('output')}")
        if "maoding-ok" not in str(results[0].get("output")):
            _fail(f"命令输出不正确：{results[0].get('output')}")
        _pass("允许后命令真的执行了，输出被回灌给模型")


def test_rpc_dangerous_command_never_asks(tmp: Path) -> None:
    print("test_rpc_dangerous_command_never_asks")
    script = [
        tool_call_response("run_shell", {"command": "rm -rf /"}),
        text_response("明白，不执行。"),
    ]
    # 即使是 auto 模式，黑名单命中的命令也必须直接拒绝，不该弹确认
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="auto") as rpc:
        rpc.send(cmd="run", prompt="删掉根目录")
        done = rpc.wait_for("done")
        rpc.drain(0.3)

        if rpc.events_of("needs_confirm"):
            _fail("危险命令不该询问，应当直接拒绝")
        results = rpc.events_of("tool_result")
        if not results or results[0].get("ok"):
            _fail(f"危险命令没有被拦下：{results}")
        if "权限拒绝" not in str(results[0].get("output")):
            _fail(f"拒绝原因未回灌：{results[0].get('output')}")
        _pass("auto 模式下危险命令仍被直接拒绝，且未产生无谓的确认交互")
        if done.get("reason") != "completed":
            _fail(f"应当继续对话，实际 {done.get('reason')}")


def test_rpc_non_stream_mode(tmp: Path) -> None:
    print("test_rpc_non_stream_mode")
    with FakeLLM([text_response("非流式回答")]) as llm, RpcProcess(
        tmp, llm.base_url, mode="auto", stream=False
    ) as rpc:
        rpc.send(cmd="run", prompt="你好")
        done = rpc.wait_for("done")
        rpc.drain(0.3)

        if rpc.events_of("delta"):
            _fail(f"--no-stream 下不应有 delta 事件：{rpc.types()}")
        if done.get("content") != "非流式回答":
            _fail(f"最终内容不对：{done.get('content')!r}")
        _pass("--no-stream 下无增量事件，正文在 done 里一次给出")


def test_rpc_introspection_commands(tmp: Path) -> None:
    print("test_rpc_introspection_commands")
    with FakeLLM([]) as llm, RpcProcess(tmp, llm.base_url, mode="auto") as rpc:
        rpc.send(cmd="tools")
        info = rpc.wait_for("info")
        if "read_file" not in info.get("message", ""):
            _fail(f"tools 命令输出不含 read_file：{info.get('message', '')[:200]}")
        _pass("tools 命令返回工具清单")

        rpc.send(cmd="policy")
        info = rpc.wait_for("info")
        if "权限模式" not in info.get("message", ""):
            _fail("policy 命令输出异常")
        _pass("policy 命令返回权限策略")

        rpc.send(cmd="config")
        cfg = rpc.wait_for("config")
        if cfg.get("data", {}).get("model", {}).get("api_key") == "fake-key":
            _fail("config 命令泄漏了明文密钥")
        _pass("config 命令返回配置且已脱敏")

        rpc.send(cmd="unknown_command")
        err = rpc.wait_for("error")
        if "未知命令" not in err.get("message", ""):
            _fail(f"未知命令未报错：{err}")
        _pass("未知命令返回 error 而不是崩溃")

        rpc.send(cmd="clear")
        rpc.wait_for("info")
        _pass("clear 命令可用")


def test_rpc_rejects_unknown_tool_gracefully(tmp: Path) -> None:
    print("test_rpc_rejects_unknown_tool_gracefully")
    script = [
        tool_call_response("no_such_tool", {"x": 1}),
        text_response("换个方式。"),
    ]
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="auto") as rpc:
        rpc.send(cmd="run", prompt="调用一个不存在的工具")
        done = rpc.wait_for("done")
        if done.get("reason") != "completed":
            _fail(f"未知工具不应中断循环，实际 {done.get('reason')}")
        rpc.drain(0.3)
        results = rpc.events_of("tool_result")
        if not results or results[0].get("ok"):
            _fail(f"未知工具应报失败：{results}")
        if "不存在" not in str(results[0].get("output")):
            _fail(f"错误信息不够明确：{results[0].get('output')}")
        _pass("未知工具被降级成回灌信息，循环继续")


def test_rpc_missing_api_key_reports_fatal(tmp: Path) -> None:
    print("test_rpc_missing_api_key_reports_fatal")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("AI_", "MOD_")):
            env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [sys.executable, "-m", "maodingcode", "--rpc", "-C", str(tmp)],
        cwd=str(IMPORT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    if proc.returncode == 0:
        _fail("缺少 API Key 时应当以非零码退出")
    first = (proc.stdout or "").strip().splitlines()[:1]
    if not first:
        _fail(f"启动失败也要用协议格式报出去，实际 stdout 为空。stderr：{proc.stderr[:200]}")
    payload = json.loads(first[0])
    if payload.get("type") != "fatal":
        _fail(f"应当是 fatal 事件：{payload}")
    _pass(f"启动失败以协议格式上报：{payload.get('message', '')[:50]}")


def test_rpc_protocol_contract_for_frontend(tmp: Path) -> None:
    """契约测试：编辑器插件依赖的事件类型，协议端必须真的都会发。

    这类测试的价值在于**跨语言的约定一旦漂移，是没有编译器帮你发现的**。
    JS 那边 `main.js` 的 switch 分支期待这些事件，Python 这边改了名字，
    结果只会是"界面某块永远不更新"，而且不报错。所以在这里钉死。

    对应 vscode-extension/media/main.js 的事件分支。
    """
    print("test_rpc_protocol_contract_for_frontend")
    required = {
        "ready",
        "delta",
        "model_response",
        "tool_start",
        "tool_result",
        "needs_confirm",
        "done",
        "info",
        "error",
    }
    observed: set[str] = set()

    # 场景一：suggest 模式下跑一个需要确认的任务，覆盖大部分类型
    script = [
        tool_call_response("run_shell", {"command": "echo contract"}),
        text_response("契约测试完成。"),
    ]
    with FakeLLM(script) as llm, RpcProcess(tmp, llm.base_url, mode="suggest") as rpc:
        rpc.send(cmd="run", prompt="契约测试")
        ask = rpc.wait_for("needs_confirm", timeout=30)
        rpc.send(cmd="confirm_response", id=ask["id"], allow=True)
        rpc.wait_for("done")
        rpc.drain(0.3)
        observed.update(rpc.types())

        # 顺带验证确认事件里前端需要用到的字段都在
        for field in ("id", "tool", "args", "reason"):
            if field not in ask:
                _fail(f"needs_confirm 缺少字段 {field}：{ask}")
        _pass("needs_confirm 带齐 id / tool / args / reason（前端要用来渲染按钮）")

        done_event = rpc.events_of("done")[0]
        for field in ("reason", "content", "steps", "tool_calls", "cost"):
            if field not in done_event:
                _fail(f"done 缺少字段 {field}：{done_event}")
        _pass("done 带齐 reason / content / steps / tool_calls / cost")

        start_event = rpc.events_of("tool_start")[0]
        if "name" not in start_event or "args" not in start_event:
            _fail(f"tool_start 缺少字段：{start_event}")
        result_event = rpc.events_of("tool_result")[0]
        for field in ("name", "ok", "output"):
            if field not in result_event:
                _fail(f"tool_result 缺少字段 {field}：{result_event}")
        _pass("tool_start / tool_result 字段满足前端渲染需求")

    # 场景二：info 与 error 走的是命令通道
    with FakeLLM([]) as llm, RpcProcess(tmp, llm.base_url, mode="auto") as rpc:
        rpc.send(cmd="tools")
        rpc.wait_for("info")
        rpc.send(cmd="不存在的命令")
        rpc.wait_for("error")
        rpc.drain(0.3)
        observed.update(rpc.types())

    missing = required - observed
    if missing:
        _fail(f"前端依赖但这些事件从未出现：{sorted(missing)}\n实际观测到：{sorted(observed)}")
    _pass(f"前端依赖的 {len(required)} 种事件全部可复现：{', '.join(sorted(required))}")


def main() -> int:
    print(f"\nRPC 端到端测试\n{'=' * 52}")
    cases = [
        test_rpc_streaming_full_flow,
        test_rpc_event_order_and_streaming,
        test_rpc_permission_denied,
        test_rpc_permission_allowed,
        test_rpc_dangerous_command_never_asks,
        test_rpc_non_stream_mode,
        test_rpc_introspection_commands,
        test_rpc_rejects_unknown_tool_gracefully,
        test_rpc_protocol_contract_for_frontend,
        test_rpc_missing_api_key_reports_fatal,
    ]
    passed = 0
    try:
        for fn in cases:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d).resolve())
            passed += 1
    except AssertionError as exc:
        # 必须把消息打出来——wait_for 抛的断言里带着 stderr，是最关键的排查线索
        if str(exc):
            print(f"\n{exc}")
        print(f"\n失败。已通过 {passed}/{len(cases)} 个用例。")
        return 1
    except KeyboardInterrupt:
        print(f"\n被中断。已通过 {passed}/{len(cases)} 个用例。")
        return 1
    print(f"{'=' * 52}\n全部 {passed} 个用例通过 ✅\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

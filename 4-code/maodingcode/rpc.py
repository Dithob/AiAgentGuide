"""NDJSON 协议模式 —— 给编辑器插件/前端当后端的入口。

    python -m maodingcode --rpc

**为什么用 NDJSON 而不是 HTTP 服务**

编辑器插件要驱动一个本地 Agent 进程，需要的其实是"双向流"：
前端推命令进去、后端推事件出来。用 HTTP 就得额外设计 SSE + 轮询两条路，
还要处理端口占用、鉴权、进程孤儿。而 NDJSON over stdio 天然具备：

  - 双向、有序、无需端口；
  - 进程随编辑器窗口存亡，不会留孤儿进程；
  - 没有网络暴露面，不用做鉴权（只有父进程能读到这个管道）。

协议：**一行一个 JSON**，两个方向都是。

    前端 → 后端（stdin）        后端 → 前端（stdout）
    {"cmd":"run","prompt":"..."}   {"type":"delta","text":"..."}
    {"cmd":"abort"}                {"type":"tool_start","name":...}
    {"cmd":"confirm_response",...} {"type":"needs_confirm",...}
    {"cmd":"tools"}                {"type":"info","tools":"..."}
                                   {"type":"done","reason":...}

**线程模型**：主线程只负责读 stdin 并分发；`loop.run()` 跑在 worker 线程里。
因为 `run()` 是同步阻塞的，如果占着主线程，`abort` 和 `confirm_response`
就永远读不到——权限确认会直接死锁。

**stdout 卫生**：协议占用了 stdout，任何一句意外的 `print()` 都会污染流。
所以启动时把 `sys.stdout` 换成 stderr，协议消息写进保存下来的原始句柄。
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from typing import Any, TextIO

from .session import Session

# 等待前端确认的超时。超时即拒绝（fail-closed），避免进程永久挂着。
CONFIRM_TIMEOUT = 300.0


class RpcServer:
    def __init__(self, session: Session, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self.session = session
        self._in: TextIO = stdin or sys.stdin
        self._out: TextIO = stdout or sys.stdout
        self._write_lock = threading.Lock()

        self._worker: threading.Thread | None = None
        self._loop = None

        self._confirm_lock = threading.Lock()
        self._confirm_event = threading.Event()
        self._confirm_target: str | None = None
        self._confirm_result = False
        self._allow_all = False

    # ------------------------------------------------------------------ 发送
    def send(self, **payload: Any) -> None:
        """把一条事件写到 stdout。多线程共享，必须加锁。"""
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError):
                # 管道已关闭（编辑器窗口关了），静默退出即可
                pass

    # ------------------------------------------------------------------ 主循环
    def serve(self) -> int:
        # 抢占 stdout：后面任何 stray print 都去 stderr，不会污染协议流
        saved_stdout = sys.stdout
        sys.stdout = sys.stderr

        self.send(
            type="ready",
            model=f"{self.session.cfg.provider.name}/{self.session.cfg.provider.model}",
            workspace=str(self.session.workspace),
            permission_mode=self.session.cfg.security.permission_mode,
            streaming=self.session.provider.supports_streaming(),
            tools=len(self.session.registry.names()),
            skills=len(self.session.skills.skills),
        )
        if self.session.mcp.errors:
            self.send(type="warning", message="部分 MCP Server 未连接", detail=self.session.mcp.errors)

        try:
            for raw in self._in:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.send(type="error", message=f"请求不是合法 JSON：{exc}")
                    continue
                if not isinstance(message, dict):
                    self.send(type="error", message="请求必须是 JSON 对象")
                    continue
                if self._dispatch(message):
                    break
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self._join_worker()
            self.session.close()
            self.send(type="bye")
            sys.stdout = saved_stdout
        return 0

    def _join_worker(self, timeout: float = 10.0) -> None:
        if self._worker and self._worker.is_alive():
            if self._loop is not None:
                self._loop.abort()
            # 唤醒可能正在等确认的 worker
            with self._confirm_lock:
                self._confirm_result = False
                self._confirm_event.set()
            self._worker.join(timeout=timeout)

    # ------------------------------------------------------------------ 分发
    def _dispatch(self, msg: dict[str, Any]) -> bool:
        """返回 True 表示要退出。"""
        cmd = str(msg.get("cmd") or "").strip()
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            self.send(type="error", message=f"未知命令：{cmd!r}")
            return False
        return bool(handler(msg))

    # ------------------------------------------------------------------ 命令
    def _cmd_ping(self, msg: dict[str, Any]) -> bool:
        self.send(type="pong", echo=msg.get("echo"))
        return False

    def _cmd_run(self, msg: dict[str, Any]) -> bool:
        prompt = str(msg.get("prompt") or "").strip()
        if not prompt:
            self.send(type="error", message="prompt 不能为空")
            return False
        if self._worker is not None and self._worker.is_alive():
            self.send(type="error", message="上一轮还没结束，请先 abort 或等待 done")
            return False

        self._allow_all = bool(msg.get("allow_all") or False)
        self._worker = threading.Thread(
            target=self._worker_main, args=(prompt,), name="maoding-rpc-worker", daemon=True
        )
        self._worker.start()
        return False

    def _cmd_abort(self, _msg: dict[str, Any]) -> bool:
        if self._loop is not None:
            self._loop.abort()
        # 顺手把等待中的确认放掉，否则 abort 之后 worker 还卡在那儿
        with self._confirm_lock:
            self._confirm_result = False
            self._confirm_event.set()
        self.send(type="aborting")
        return False

    def _cmd_confirm_response(self, msg: dict[str, Any]) -> bool:
        cid = msg.get("id")
        with self._confirm_lock:
            if self._confirm_target is None or cid != self._confirm_target:
                self.send(type="error", message="没有等待中的确认请求（可能已超时）")
                return False
            self._confirm_result = bool(msg.get("allow"))
            if msg.get("all"):
                self._allow_all = True
            self._confirm_event.set()
        return False

    def _cmd_clear(self, _msg: dict[str, Any]) -> bool:
        self.session.context.clear()
        self.send(type="info", message="已清空对话历史")
        return False

    def _cmd_tools(self, _msg: dict[str, Any]) -> bool:
        self.send(type="info", message=self.session.registry.describe())
        return False

    def _cmd_skills(self, _msg: dict[str, Any]) -> bool:
        self.send(type="info", message=self.session.skills.describe())
        return False

    def _cmd_mcp(self, _msg: dict[str, Any]) -> bool:
        self.send(type="info", message=self.session.mcp.describe())
        return False

    def _cmd_policy(self, _msg: dict[str, Any]) -> bool:
        self.send(type="info", message=self.session.policy.describe())
        return False

    def _cmd_config(self, _msg: dict[str, Any]) -> bool:
        from .config import config_as_dict

        self.send(type="config", data=config_as_dict(self.session.cfg))
        return False

    def _cmd_cost(self, _msg: dict[str, Any]) -> bool:
        self.send(type="cost", data=self.session.tracer.summary())
        return False

    def _cmd_memory(self, msg: dict[str, Any]) -> bool:
        action = str(msg.get("action") or "read")
        store = self.session.memory
        if action == "read":
            self.send(type="info", message=store.load_project_memory() or "(MAODING.md 为空)")
        elif action == "init":
            path = store.ensure_project_memory()
            self.session.refresh_system_prompt()
            self.send(type="info", message=f"已创建 {path}")
        elif action == "append":
            store.append_note(str(msg.get("note") or ""), section=str(msg.get("section") or "前端记录"))
            self.session.refresh_system_prompt()
            self.send(type="info", message="已追加并刷新系统提示")
        else:
            self.send(type="error", message=f"未知 action：{action}")
        return False

    def _cmd_shutdown(self, _msg: dict[str, Any]) -> bool:
        return True

    # ------------------------------------------------------------------ worker
    def _worker_main(self, prompt: str) -> None:
        def emit(event) -> None:
            payload = dict(event.payload)
            # usage 对象不能直接 JSON 序列化，摊平成数字
            usage = payload.pop("usage", None)
            if usage is not None:
                payload["usage"] = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "total_tokens": usage.total_tokens,
                }
            stats = payload.pop("stats", None)
            if stats is not None:
                payload["stats"] = {"total_tokens": stats.total, "folded": stats.folded_messages}
            self.send(type=event.kind, **payload)

        self._loop = self.session.make_loop(emit=emit, confirm=self._ask_permission)
        try:
            result, summary = self.session.run_turn(prompt, emit=emit, loop=self._loop)
        except Exception as exc:  # 兜底：任何意外都不能让进程静默卡死
            self.send(type="error", message=f"{type(exc).__name__}: {exc}")
            self.send(type="done", reason="error", content="")
            self._loop = None
            return

        self.send(
            type="done",
            reason=result.reason.value,
            content=result.content,
            steps=result.steps,
            tool_calls=result.tool_calls_made,
            cost=summary,
        )
        self._loop = None

    # ------------------------------------------------------------------ 确认
    def _ask_permission(self, tool_name: str, args: dict, reason: str) -> bool:
        """在 worker 线程里被调用：发问询事件，然后阻塞等前端回答。

        这里必须阻塞——权限决策是同步的语义，如果直接返回 False，
        等于所有需要确认的操作都被自动拒绝了，`suggest` 模式就废了。
        """
        if self._allow_all:
            return True

        cid = uuid.uuid4().hex[:12]
        with self._confirm_lock:
            self._confirm_target = cid
            self._confirm_result = False
            self._confirm_event.clear()

        self.send(type="needs_confirm", id=cid, tool=tool_name, args=args, reason=reason)

        got = self._confirm_event.wait(timeout=CONFIRM_TIMEOUT)
        with self._confirm_lock:
            self._confirm_target = None
            allowed = self._confirm_result
            self._confirm_event.clear()

        if not got:
            self.send(type="warning", message=f"等待确认超时（{CONFIRM_TIMEOUT:.0f}s），按拒绝处理")
        return allowed


def _emit_fatal(message: str) -> None:
    """启动失败也要用协议格式报出去。

    否则前端只会看到"进程以非零码退出了"，完全不知道为什么。
    """
    print(json.dumps({"type": "fatal", "message": message}, ensure_ascii=False), flush=True)


def serve_from_args(args: Any) -> int:
    """用**已经解析好的 CLI 参数**启动协议模式。

    刻意不在 rpc 里另建一套 argparse：参数定义一旦有两份，
    迟早出现"CLI 有 --no-stream 但 RPC 没有"这类漂移
    （写测试时真的踩到了）。统一复用 `cli.build_parser`。
    """
    from .cli import build_overrides
    from .config import load_config
    from .errors import MaodingError

    try:
        cfg = load_config(workspace=args.workspace, overrides=build_overrides(args))
        session = Session(cfg)
    except MaodingError as exc:
        _emit_fatal(str(exc))
        return 2
    except Exception as exc:  # 兜底：初始化期的意外也要报出去
        _emit_fatal(f"{type(exc).__name__}: {exc}")
        return 2
    return RpcServer(session).serve()


def main(argv: list[str] | None = None) -> int:
    """独立入口：`python -m maodingcode.rpc` / `python -m maodingcode --rpc`"""
    from .cli import build_parser

    args = build_parser().parse_args(argv)
    return serve_from_args(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

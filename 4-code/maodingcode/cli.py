"""命令行入口。

装配逻辑在 `session.py`，这里只负责三件事：
读输入、把事件渲染成终端文本、处理斜杠命令。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import AppConfig, config_as_dict, load_config
from .core.loop import LoopEvent
from .errors import MaodingError
from .session import Session
from .ui import render


class ChatCli:
    """终端宿主。持有 Session，负责输入输出。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.cfg: AppConfig = session.cfg
        self._auto_approve = False
        # 本轮是否已经吐过流式增量——决定最终文本还要不要再打印一次
        self._streamed_any = False

    # ------------------------------------------------------------------ 事件
    def _on_event(self, event: LoopEvent) -> None:
        kind = event.kind
        payload = event.payload

        if kind == "delta":
            # 流式正文直接吐，不换行不加前缀，让文字像打字一样出现
            sys.stdout.write(str(payload.get("text") or ""))
            sys.stdout.flush()
            self._streamed_any = True
            return

        if kind == "assistant_message":
            return  # 正文由 _run_turn 统一收尾

        if kind == "tool_result":
            print(render.render_event(kind, payload) or "")
            if not payload.get("ok"):
                print(render.tool_output_preview(str(payload.get("output", ""))))
            elif self.cfg.debug:
                print(render.tool_output_preview(str(payload.get("output", "")), limit=6))
            return

        text = render.render_event(kind, payload)
        if text:
            print(text)

    def _confirm(self, tool_name: str, args: dict, reason: str) -> bool:
        if self._auto_approve:
            return True
        print(render.confirm_prompt(tool_name, args, reason))
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in {"a", "all"}:
            self._auto_approve = True
            return True
        return answer in {"y", "yes"}

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        s = self.session
        print(
            render.banner(
                __version__,
                f"{self.cfg.provider.name}/{self.cfg.provider.model}",
                str(s.workspace),
                self.cfg.security.permission_mode,
            )
        )
        for name, err in s.mcp.errors.items():
            print(render.warn(f"MCP Server {name} 未连接：{err}"))
        if not s.skills.skills:
            print(render.hint("未发现技能。可在 skills/ 或 .maodingcode/skills/ 下放 SKILL.md"))
        if s.provider.supports_streaming():
            print(render.hint("流式输出已开启。"))
        print(render.hint("提示：项目约定写在 MAODING.md 里，每轮都会读进系统提示。\n"))

        while True:
            try:
                raw = input(render.user_prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = raw.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self._handle_command(line):
                    break
                continue
            self._run_turn(line)

        self.session.close()
        print(render.dim("\n再见。"))
        return 0

    def _run_turn(self, user_input: str) -> None:
        # 每轮重置：auto-approve 与流式状态都不应跨轮泄漏
        self._auto_approve = False
        self._streamed_any = False

        loop = self.session.make_loop(emit=self._on_event, confirm=self._confirm)
        try:
            result, summary = self.session.run_turn(user_input, emit=self._on_event, loop=loop)
        except KeyboardInterrupt:
            print(render.warn("已中断。"))
            return

        if result.content:
            if self._streamed_any:
                print()  # 正文已经流式吐过了，补个空行即可
            elif result.reason.value == "completed":
                print(render.assistant(result.content))
            else:
                print(render.warn(result.content))

        if self.cfg.debug or result.tool_calls_made:
            print(render.cost_report(summary))

    # ------------------------------------------------------------------ 斜杠命令
    def _handle_command(self, line: str) -> bool:
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        s = self.session

        if cmd in {"/quit", "/exit", "/q"}:
            return True
        if cmd == "/help":
            print(HELP_TEXT)
        elif cmd == "/tools":
            print(s.registry.describe())
        elif cmd == "/skills":
            print(s.skills.describe())
        elif cmd == "/mcp":
            print(s.mcp.describe())
        elif cmd == "/policy":
            print(s.policy.describe())
        elif cmd == "/config":
            print(json.dumps(config_as_dict(self.cfg), ensure_ascii=False, indent=2))
        elif cmd == "/memory":
            self._cmd_memory(args)
        elif cmd == "/clear":
            s.context.clear()
            print(render.hint("已清空对话历史（项目约定不受影响）。"))
        elif cmd == "/save":
            name = args[0] if args else "session"
            path = s.memory.save_session(
                name, s.context.messages, {"model": self.cfg.provider.model}
            )
            print(render.hint(f"已保存到 {path}"))
        elif cmd == "/load":
            self._cmd_load(args)
        elif cmd == "/cost":
            print(render.cost_report(s.tracer.summary()))
        else:
            print(render.warn(f"未知命令 {cmd}，输入 /help 查看可用命令。"))
        return False

    def _cmd_load(self, args: list[str]) -> None:
        s = self.session
        if not args:
            latest = s.memory.latest_session()
            if not latest:
                print(render.warn("没有可恢复的会话。"))
                return
            args = [latest]
        messages = s.memory.load_session(args[0])
        if not messages:
            print(render.warn(f"会话 {args[0]} 不存在或为空。"))
            return
        s.context.clear()
        s.context.extend(messages)
        print(render.hint(f"已恢复 {len(messages)} 条消息。"))

    def _cmd_memory(self, args: list[str]) -> None:
        s = self.session
        if not args:
            text = s.memory.load_project_memory()
            print(
                text
                or render.hint(f"还没有 {s.memory.memory_path.name}，可用 `/memory init` 创建。")
            )
            return
        if args[0] == "init":
            path = s.memory.ensure_project_memory()
            s.refresh_system_prompt()
            print(render.hint(f"已创建 {path}"))
            return
        s.memory.append_note(" ".join(args), section="手动记录")
        s.refresh_system_prompt()
        print(render.hint(f"已追加到 {s.memory.memory_path.name} 并刷新系统提示。"))


HELP_TEXT = """
命令：
  /help            显示本帮助
  /tools           列出已注册工具（内置 + MCP + 技能）
  /skills          列出已加载技能及其路径
  /mcp             查看 MCP Server 连接状态
  /policy          查看当前权限策略（模式、工作区、危险命令规则数）
  /config          查看脱敏后的配置
  /memory          查看项目约定 MAODING.md
  /memory init     创建 MAODING.md 模板
  /memory <内容>    追加一条约定并立即生效
  /save [名称]      保存当前会话到 .maodingcode/sessions/
  /load [名称]      恢复会话（不带名称则恢复最近一次）
  /clear           清空对话历史
  /cost            查看本会话 token 与成本统计
  /quit            退出

权限模式说明（maodingcode.toml 或 MOD_PERMISSION_MODE）：
  suggest   写文件自动放行，执行命令 / 不可逆操作需确认（默认）
  auto      全部自动放行（仅在完全可信的目录里使用）
  readonly  只允许只读工具
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="maodingcode",
        description="MaoDingCode —— 本地运行的 AI 编码助手",
    )
    p.add_argument("prompt", nargs="*", help="一次性执行的提示词；留空则进入交互模式")
    p.add_argument("-C", "--workspace", default=None, help="工作区根目录，默认当前目录")
    p.add_argument("-m", "--model", default=None, help="覆盖模型名")
    p.add_argument("--base-url", default=None, help="覆盖 API base_url")
    p.add_argument("--api-key", default=None, help="覆盖 API Key")
    p.add_argument(
        "--mode",
        dest="permission_mode",
        choices=["auto", "suggest", "readonly"],
        default=None,
        help="权限模式",
    )
    p.add_argument("--max-steps", type=int, default=None, help="单轮最大步数")
    p.add_argument(
        "--rpc",
        action="store_true",
        help="以 NDJSON 协议模式运行（供编辑器插件调用）：stdin 收命令，stdout 发事件",
    )
    p.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    p.add_argument(
        "--allow",
        dest="allow_all",
        action="store_true",
        help="自动放行需要确认的操作（非交互场景使用）",
    )
    p.add_argument("--debug", action="store_true", help="打印详细过程与统计")
    p.add_argument("-v", "--version", action="version", version=f"MaoDingCode {__version__}")
    return p


def build_overrides(args: argparse.Namespace) -> dict:
    return {
        "workspace": args.workspace,
        "model": args.model,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "permission_mode": "auto" if args.allow_all else args.permission_mode,
        "max_steps": args.max_steps,
        "debug": args.debug,
        "stream": not args.no_stream,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # --rpc 交给协议入口：它要把 stdout 整个让给协议，不能和 CLI 的打印混在一起。
    # 传 args 而不是重新解析 argv，保证两边参数定义永远一致。
    if args.rpc:
        from .rpc import serve_from_args

        return serve_from_args(args)

    try:
        cfg = load_config(workspace=args.workspace, overrides=build_overrides(args))
    except MaodingError as exc:
        print(render.error(str(exc)), file=sys.stderr)
        print(render.hint("参考 .env.example 与 maodingcode.toml 补齐配置。"), file=sys.stderr)
        return 2

    try:
        session = Session(cfg)
    except MaodingError as exc:
        print(render.error(f"初始化失败：{exc}"), file=sys.stderr)
        return 2

    # 带参数 = 一次性执行，适合脚本与 CI；无参数 = 交互模式
    if args.prompt:
        return _run_once(session, " ".join(args.prompt))

    return ChatCli(session).run()


def _run_once(session: Session, prompt: str) -> int:
    """非交互式单发。

    不传 confirm：没人在旁边点"同意"，所以需要确认的操作一律拒绝。
    要放开就用 `--allow`（等价于 permission_mode=auto）。
    这个默认值是刻意的——CI 里静默执行危险操作比失败更糟。
    """
    streamed = {"any": False}

    def emit(event: LoopEvent) -> None:
        kind = event.kind
        if kind == "delta":
            sys.stdout.write(str(event.payload.get("text") or ""))
            sys.stdout.flush()
            streamed["any"] = True
        elif kind in {"tool_result", "error"}:
            # 过程信息走 stderr，stdout 只留最终答案，方便 `cmd > out.txt` 取结果
            text = render.render_event(kind, event.payload)
            if text:
                print(text, file=sys.stderr)

    loop = session.make_loop(emit=emit, confirm=None)
    result, _summary = session.run_turn(prompt, emit=emit, loop=loop)
    session.close()

    if result.content:
        print() if streamed["any"] else None
        if not streamed["any"]:
            print(result.content)
    elif result.reason.value != "completed":
        print(render.error(result.content or "未产生输出"), file=sys.stderr)

    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

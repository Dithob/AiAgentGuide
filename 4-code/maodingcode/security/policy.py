"""权限与可逆性分级。

一个 coding agent 最危险的时刻，是它"很有把握地"执行了一条破坏性命令。
所以权限层要回答三个问题：

  1. 这个工具会改变什么？（只读 / 可逆写 / 不可逆）
  2. 它作用在哪儿？（工作区内 / 工作区外 / 系统路径）
  3. 这个动作能不能撤销？（能 -> 直接做；不能 -> 必须问）

落到代码里就是一个查表 + 几条硬规则：

    只读工具          -> ALLOW
    写工作区内文件    -> 按 permission_mode 决定
    写工作区外文件    -> 一律 ASK（除非显式配置了可写目录）
    执行 shell        -> 命中黑名单 DENY；其余按模式
    匹配到危险模式    -> DENY（即使 mode=auto）

刻意不做成"模型自己判断危不危险"——模型可以说服自己任何事。
策略必须是确定性的、可审计的、模型改不动的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..config import SecurityConfig
from ..errors import PermissionDenied


class Verdict(str, Enum):
    ALLOW = "allow"   # 直接执行
    ASK = "ask"       # 需要用户确认
    DENY = "deny"     # 直接拒绝


class Reversibility(str, Enum):
    READ_ONLY = "read_only"     # 无副作用
    REVERSIBLE = "reversible"   # 有副作用但可回滚（改文件、建目录）
    IRREVERSIBLE = "irreversible"  # 难以回滚（删除、推送、发网络请求）


@dataclass(slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    reversibility: Reversibility = Reversibility.READ_ONLY

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


# 命令级黑名单：命中即拒，与 permission_mode 无关。
# 只列"几乎没有正当理由让 Agent 自动执行"的模式。
DEFAULT_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", "递归强制删除"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\s+.*of=/dev/", "裸设备写入"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork 炸弹"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "关机/重启"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "根目录降权"),
    (r">\s*/dev/sd[a-z]", "直接覆写磁盘"),
    (r"\bgit\s+push\s+.*--force", "强推覆盖远端历史"),
    (r"\bgit\s+reset\s+--hard\b", "丢弃全部未提交改动"),
    (r"\bgit\s+clean\s+-\w*f", "清理未跟踪文件"),
    (r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba|z|)sh\b", "管道执行远程脚本"),
    (r"\b(wget|curl)\b.*-O\s*/(etc|usr|bin|boot)/", "下载覆盖系统文件"),
    (r"\buserdel\b|\bgroupdel\b", "删除系统用户"),
    (r"\b(iptables|ufw|firewall-cmd)\b", "修改防火墙规则"),
    (r"\bcrontab\s+-r\b", "清空定时任务"),
    (r"\bhistory\s+-c\b", "清除操作历史"),
)

# 命令名 -> 场景标签，用于给确认提示补充说明
SHELL_TOOL_NAMES = {"run_shell", "bash", "shell", "execute_command"}


@dataclass
class Policy:
    """确定性权限策略。模型无法绕过，因为它根本不参与决策。"""

    config: SecurityConfig
    tool_reversibility: dict[str, Reversibility] = field(default_factory=dict)
    extra_deny_patterns: tuple[tuple[str, str], ...] = ()

    _compiled: list[tuple[re.Pattern[str], str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        patterns = DEFAULT_DENY_PATTERNS + tuple(
            (p, "自定义黑名单") for p in self.config.command_denylist
        )
        patterns += self.extra_deny_patterns
        self._compiled = [
            (re.compile(p, re.IGNORECASE | re.DOTALL), why) for p, why in patterns
        ]

    # ------------------------------------------------------------------ 对外
    def register_reversibility(self, tool_name: str, rev: Reversibility) -> None:
        """由工具注册中心在注册时回填，避免两边各写一份表。"""
        self.tool_reversibility[tool_name] = rev

    def check(self, tool_name: str, args: dict) -> Decision:
        """判定一次工具调用。

        Raises:
            PermissionDenied: 参数本身非法（例如路径逃逸）。
        """
        rev = self.tool_reversibility.get(tool_name, Reversibility.REVERSIBLE)

        # 1) 命令黑名单优先于一切
        if tool_name in SHELL_TOOL_NAMES:
            command = str(args.get("command") or args.get("cmd") or "")
            hit = self._match_denylist(command)
            if hit:
                return Decision(
                    Verdict.DENY,
                    f"命令命中危险模式（{hit}），已阻止执行。如确需执行请手动运行。",
                    Reversibility.IRREVERSIBLE,
                )

        # 2) 路径必须落在允许范围内
        path_arg = args.get("path") or args.get("file_path")
        if path_arg:
            resolved = self.resolve_path(str(path_arg))
            inside = self.is_inside_workspace(resolved)

            if tool_name in {"write_file", "edit_file", "create_dir"} and not inside:
                if not self._is_extra_writable(resolved):
                    return Decision(
                        Verdict.ASK,
                        f"目标路径在工作区之外：{resolved}。写工作区外文件需要确认。",
                        Reversibility.REVERSIBLE,
                    )
            if tool_name in {"read_file", "list_dir", "glob_files"} and not inside:
                return Decision(
                    Verdict.ASK,
                    f"读取工作区之外的路径：{resolved}",
                    Reversibility.READ_ONLY,
                )

        # 3) 按模式与可逆性分级
        mode = self.config.permission_mode
        if rev is Reversibility.READ_ONLY:
            return Decision(Verdict.ALLOW, "只读操作", rev)

        if mode == "readonly":
            return Decision(Verdict.DENY, f"当前为只读模式，不允许 {tool_name}", rev)

        if mode == "auto":
            return Decision(Verdict.ALLOW, "auto 模式放行", rev)

        # mode == suggest
        if rev is Reversibility.IRREVERSIBLE:
            return Decision(Verdict.ASK, f"{tool_name} 不可逆，需要确认", rev)
        if tool_name in SHELL_TOOL_NAMES:
            return Decision(Verdict.ASK, "执行 shell 命令需要确认", rev)
        if tool_name in {"write_file", "edit_file"}:
            return Decision(Verdict.ALLOW, "工作区内文件修改自动放行", rev)
        return Decision(Verdict.ASK, f"{tool_name} 需要确认", rev)

    # ------------------------------------------------------------------ 路径
    def resolve_path(self, raw: str) -> Path:
        """解析路径并拒绝恶意的绝对/向上逃逸写法。

        Raises:
            PermissionDenied: 路径包含 NUL 等非法字符。
        """
        if "\x00" in raw:
            raise PermissionDenied("路径包含非法字符", reason="NUL 字节")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.config.workspace_root / p
        try:
            return p.resolve()
        except OSError as exc:  # 循环链接等
            raise PermissionDenied(f"无法解析路径：{raw}", reason=str(exc)) from exc

    def is_inside_workspace(self, path: Path) -> bool:
        """注意：必须比较 resolve 之后的路径，否则 `../` 能绕过。"""
        root = self.config.workspace_root.resolve()
        try:
            path.resolve().relative_to(root)
            return True
        except ValueError:
            return False

    def _is_extra_writable(self, path: Path) -> bool:
        for extra in self.config.writable_extra:
            try:
                path.resolve().relative_to(extra.resolve())
                return True
            except ValueError:
                continue
        return False

    # ------------------------------------------------------------------ 命令
    def _match_denylist(self, command: str) -> str | None:
        for pattern, why in self._compiled:
            if pattern.search(command):
                return why
        return None

    def describe(self) -> str:
        cfg = self.config
        return (
            f"权限模式：{cfg.permission_mode}\n"
            f"工作区：{cfg.workspace_root}\n"
            f"可写目录：{', '.join(str(p) for p in cfg.writable_extra) or '（仅工作区）'}\n"
            f"危险命令规则：{len(self._compiled)} 条"
        )


def reversibility_of(tool) -> Reversibility:
    """从工具对象上读可逆性，缺省按可逆处理（保守）。"""
    declared = getattr(tool, "reversibility", None)
    if isinstance(declared, Reversibility):
        return declared
    return Reversibility.REVERSIBLE if not getattr(tool, "read_only", False) else Reversibility.READ_ONLY


__all__ = [
    "Policy",
    "Decision",
    "Verdict",
    "Reversibility",
    "DEFAULT_DENY_PATTERNS",
    "reversibility_of",
    "SHELL_TOOL_NAMES",
]

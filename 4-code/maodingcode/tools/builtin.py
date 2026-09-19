"""内置工具集。

工具设计的几条经验：

  - **描述里写"什么时候用"，不只写"是什么"。** 模型靠描述做选择，
    写清适用场景比堆参数说明有效得多。
  - **错误要可自愈。** 文件不存在时把"当前目录里有什么"一起返回，
    模型下一轮就能改对路径，省掉一整轮往返。
  - **输出要有界。** 读一个 10 万行的文件会把上下文直接打爆，
    所以一律带截断 + 提示"用 offset/limit 继续读"。
  - **写入尽量可逆。** edit_file 用精确字符串替换而不是行号，
    避免模型数错行导致改错地方。
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import time
from pathlib import Path

from ..security.policy import Reversibility
from .base import Tool, tool

MAX_READ_LINES = 2_000
MAX_OUTPUT_CHARS = 24_000
MAX_SEARCH_RESULTS = 200


# --------------------------------------------------------------------- 读文件
@tool(
    name="read_file",
    description=(
        "读取工作区中某个文本文件的内容，带行号。\n"
        "什么时候用：需要修改某个文件之前、需要确认某个实现细节时。\n"
        "注意：大文件请配合 offset/limit 分段读，不要一次读整个文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对工作区根目录"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始"},
            "limit": {"type": "integer", "description": f"最多读取行数，默认 {MAX_READ_LINES}"},
        },
        "required": ["path"],
    },
    read_only=True,
    reversibility=Reversibility.READ_ONLY,
)
def read_file(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    p = _resolve(path)
    if p.is_dir():
        return f"{path} 是目录。用 list_dir 查看内容。\n{_listing(p)}"
    if not p.is_file():
        return f"文件不存在：{path}\n{_sibling_hint(p)}"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取失败：{exc}"

    lines = text.splitlines()
    start = max(int(offset), 1) - 1
    end = min(start + max(int(limit), 1), len(lines))
    if start >= len(lines):
        return f"offset={offset} 超出文件范围（共 {len(lines)} 行）"

    width = len(str(end))
    body = "\n".join(f"{i + 1:>{width}}\t{lines[i]}" for i in range(start, end))
    trailer = ""
    if end < len(lines):
        trailer = f"\n... 还有 {len(lines) - end} 行，用 offset={end + 1} 继续读"
    return f"{path}（共 {len(lines)} 行，显示 {start + 1}-{end}）\n{body}{trailer}"


# --------------------------------------------------------------------- 写文件
@tool(
    name="write_file",
    description=(
        "把内容完整写入一个文件；文件已存在则覆盖。\n"
        "什么时候用：新建文件，或整体重写一个小文件。\n"
        "注意：修改已有文件请优先用 edit_file，避免覆盖掉你没读到的部分。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    },
    reversibility=Reversibility.REVERSIBLE,
)
def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    existed = p.exists()
    if existed and p.is_dir():
        return f"{path} 是目录，不能写入"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"写入失败：{exc}"
    action = "覆盖" if existed else "新建"
    return f"已{action} {path}，共 {len(content)} 字符 / {len(content.splitlines())} 行"


# --------------------------------------------------------------------- 改文件
@tool(
    name="edit_file",
    description=(
        "在文件中做精确字符串替换（找到 old_string 并换成 new_string）。\n"
        "什么时候用：修改已有文件的一小段代码。\n"
        "规则：old_string 必须在文件中唯一出现，否则会被拒绝——"
        "请带上足够多的上下文行让它唯一。要插入内容就用 new_string 带上原文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文，需唯一"},
            "new_string": {"type": "string", "description": "替换成的新内容"},
            "replace_all": {"type": "boolean", "description": "是否替换全部出现位置，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    reversibility=Reversibility.REVERSIBLE,
)
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"文件不存在：{path}\n{_sibling_hint(p)}"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return f"读取失败：{exc}"

    occurrences = text.count(old_string)
    if occurrences == 0:
        return (
            f"没有找到要替换的内容。注意 old_string 必须与实际文件字符级一致"
            f"（含缩进和空行）。文件共 {len(text.splitlines())} 行，建议先 read_file 确认。"
        )
    if occurrences > 1 and not replace_all:
        return (
            f"old_string 在文件中出现了 {occurrences} 次，不唯一。"
            f"请扩大上下文让它唯一，或设置 replace_all=true。"
        )

    updated = text.replace(old_string, new_string) if replace_all else text.replace(
        old_string, new_string, 1
    )
    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f"写入失败：{exc}"

    delta = len(updated.splitlines()) - len(text.splitlines())
    return f"已修改 {path}（替换 {occurrences if replace_all else 1} 处，行数变化 {delta:+d}）\n{_diff_preview(old_string, new_string)}"


# --------------------------------------------------------------------- 列目录
@tool(
    name="list_dir",
    description=(
        "列出目录内容（目录在前，文件在后，带大小）。\n"
        "什么时候用：进入一个新项目、猜测文件位置失败之后先看一眼结构。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认工作区根目录"},
            "depth": {"type": "integer", "description": "递归深度，默认 1"},
        },
        "required": [],
    },
    read_only=True,
    reversibility=Reversibility.READ_ONLY,
)
def list_dir(path: str = ".", depth: int = 1) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"目录不存在：{path}"
    if not p.is_dir():
        return f"{path} 不是目录"
    return _listing(p, depth=max(1, min(int(depth), 4)))


# --------------------------------------------------------------------- 找文件
@tool(
    name="glob_files",
    description=(
        "按文件名模式查找文件，如 '**/*.py'、'src/**/*.ts'。\n"
        "什么时候用：不确定某个文件在哪儿的时候。找文件内容请用 grep_files。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "搜索根目录，默认工作区根目录"},
        },
        "required": ["pattern"],
    },
    read_only=True,
    reversibility=Reversibility.READ_ONLY,
)
def glob_files(pattern: str, path: str = ".") -> str:
    root = _resolve(path)
    if not root.is_dir():
        return f"目录不存在：{path}"
    matches = [
        m for m in sorted(root.glob(pattern)) if m.is_file() and not _ignored(m)
    ]
    if not matches:
        return f"没有匹配 {pattern} 的文件（搜索根目录 {root}）"
    shown = matches[:MAX_SEARCH_RESULTS]
    lines = [str(m.relative_to(root)) for m in shown]
    more = f"\n... 还有 {len(matches) - len(shown)} 个结果" if len(matches) > len(shown) else ""
    return f"匹配 {len(matches)} 个文件：\n" + "\n".join(lines) + more


# --------------------------------------------------------------------- 搜内容
@tool(
    name="grep_files",
    description=(
        "在文件内容里搜索正则表达式，返回 文件:行号: 内容。\n"
        "什么时候用：定位某个函数/字符在哪定义、在哪被调用。\n"
        "建议先用它定位，再用 read_file 看上下文，不要靠猜。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索根目录，默认工作区根目录"},
            "glob": {"type": "string", "description": "限定文件类型，如 '*.py'"},
            "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false"},
        },
        "required": ["pattern"],
    },
    read_only=True,
    reversibility=Reversibility.READ_ONLY,
)
def grep_files(
    pattern: str, path: str = ".", glob: str = "", case_sensitive: bool = False
) -> str:
    import re

    root = _resolve(path)
    if not root.exists():
        return f"路径不存在：{path}"
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return f"正则表达式非法：{exc}"

    hits: list[str] = []
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    scanned = 0
    for f in files:
        if not f.is_file() or _ignored(f) or _looks_binary(f):
            continue
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        scanned += 1
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if regex.search(line):
                    rel = f.relative_to(root) if root.is_dir() else f.name
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_RESULTS:
                        break
        except OSError:
            continue
        if len(hits) >= MAX_SEARCH_RESULTS:
            break

    if not hits:
        return f"没有匹配 {pattern!r} 的内容（扫描 {scanned} 个文件）"
    tail = "\n... 结果已截断，请缩小范围" if len(hits) >= MAX_SEARCH_RESULTS else ""
    return f"匹配 {len(hits)} 条（扫描 {scanned} 个文件）：\n" + "\n".join(hits) + tail


# --------------------------------------------------------------------- 执行命令
@tool(
    name="run_shell",
    description=(
        "在工作区目录下执行一条 shell 命令，返回 stdout/stderr 与退出码。\n"
        "什么时候用：运行测试、构建、装依赖、跑脚本、git 状态查询。\n"
        "注意：\n"
        "- 命令会被权限策略检查，危险命令（rm -rf、强推等）会被直接拒绝；\n"
        "- 不要用 cat/sed/grep 这类命令替代 read_file/grep_files/edit_file；\n"
        "- 不要执行长时间驻留的命令（如不带 -d 的服务进程）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {"type": "string", "description": "工作目录，默认工作区根目录"},
            "timeout": {"type": "number", "description": "超时秒数，默认 60"},
        },
        "required": ["command"],
    },
    reversibility=Reversibility.IRREVERSIBLE,
)
def run_shell(command: str, cwd: str = ".", timeout: float = 60.0) -> str:
    workdir = _resolve(cwd)
    if not workdir.is_dir():
        return f"工作目录不存在：{cwd}"

    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=min(float(timeout), 600.0),
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（>{timeout}s）：{command}\n建议拆成更小的命令，或加上超时参数。"
    except OSError as exc:
        return f"命令无法启动：{exc}"

    elapsed = time.time() - started
    parts = [f"$ {command}", f"退出码：{proc.returncode}（{elapsed:.2f}s）"]
    if proc.stdout.strip():
        parts.append("--- stdout ---\n" + _clip(proc.stdout))
    if proc.stderr.strip():
        parts.append("--- stderr ---\n" + _clip(proc.stderr))
    if not proc.stdout.strip() and not proc.stderr.strip():
        parts.append("(无输出)")
    return "\n".join(parts)


# --------------------------------------------------------------------- 待办
@tool(
    name="todo_write",
    description=(
        "维护当前任务的待办清单，用于把多步任务显式化。\n"
        "什么时候用：任务包含 3 个以上步骤时，先列清单再动手；\n"
        "每完成一项就更新一次状态，避免做到一半忘了原始目标。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "完整清单，每次覆盖写入",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "任务描述"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done"],
                            "description": "状态",
                        },
                    },
                    "required": ["text", "status"],
                },
            }
        },
        "required": ["items"],
    },
    reversibility=Reversibility.READ_ONLY,
)
def todo_write(items: list) -> str:
    if not isinstance(items, list) or not items:
        return "items 必须是非空数组"
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    lines = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lines.append(f"{marks.get(str(it.get('status')), '[?]')} {it.get('text', '')}")
    done = sum(1 for it in items if isinstance(it, dict) and it.get("status") == "done")
    return f"待办进度 {done}/{len(items)}\n" + "\n".join(lines)


# --------------------------------------------------------------------- 记忆
@tool(
    name="remember",
    description=(
        "把一条需要跨会话保留的结论追加到项目约定文件 MAODING.md。\n"
        "什么时候用：用户明确说了「记住这个」，或你发现了一条以后每次都会踩的坑。\n"
        "注意：只写稳定的事实与约定，不要记临时状态。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "要记住的内容，一两句话"},
            "section": {"type": "string", "description": "归类，如 构建命令 / 代码约定 / 已知坑"},
        },
        "required": ["note"],
    },
    reversibility=Reversibility.REVERSIBLE,
)
def remember(note: str, section: str = "临时记录") -> str:
    from ..core.memory import MemoryStore

    store = MemoryStore(workspace_root=_WORKSPACE[0])
    path = store.append_note(note, section)
    return f"已追加到 {path.name}。[{section}] {note}"


# --------------------------------------------------------------------- 辅助
# 工作区根目录由 build_builtin_tools 注入，避免每个工具都多一个 workspace 参数
_WORKSPACE: list[Path] = [Path.cwd()]

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".maodingcode", "target", ".next",
}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".class"}
TEXT_HINT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".scala", ".sh",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".html", ".css", ".scss", ".sql", ".xml", ".gradle", ".properties",
}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
                   ".gz", ".tar", ".jar", ".mp3", ".mp4", ".woff", ".woff2", ".ttf"}


def _resolve(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _WORKSPACE[0] / p
    return p.resolve()


def _ignored(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    return any(part in IGNORED_DIRS for part in path.parts)


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    if path.suffix.lower() in TEXT_HINT_SUFFIXES:
        return False
    try:
        chunk = path.open("rb").read(1024)
    except OSError:
        return True
    return b"\x00" in chunk


def _listing(root: Path, depth: int = 1) -> str:
    lines: list[str] = []
    count = 0

    def walk(d: Path, level: int, prefix: str) -> None:
        nonlocal count
        if level > depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in IGNORED_DIRS or _ignored(e):
                continue
            count += 1
            if count > 300:
                return
            if e.is_dir():
                lines.append(f"{prefix}{e.name}/")
                walk(e, level + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{e.name}  ({_human_size(e)})")

    walk(root, 1, "")
    if not lines:
        return f"{root} 下没有可见文件"
    return f"{root} 的内容：\n" + "\n".join(lines)


def _human_size(p: Path) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _sibling_hint(p: Path) -> str:
    """文件没找到时，顺手给出同目录的文件列表——帮模型下一轮改对路径。"""
    parent = p.parent
    if not parent.is_dir():
        return f"父目录也不存在：{parent}"
    try:
        names = sorted(x.name for x in parent.iterdir())[:30]
    except OSError:
        return ""
    return f"{parent} 下的文件：{', '.join(names)}"


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text.rstrip()
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [中间省略 {len(text) - MAX_OUTPUT_CHARS} 字符] ...\n{text[-half:]}".rstrip()


def _diff_preview(old: str, new: str, max_lines: int = 14) -> str:
    """生成一段极简 diff 预览，让用户能一眼看出改了什么。"""
    import difflib

    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", n=1, fromfile="改前", tofile="改后"
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + ["...（diff 已截断）"]
    return "\n".join(diff)


def build_builtin_tools(workspace: Path) -> list[Tool]:
    """构造内置工具集，并把工作区根目录注入给路径相关工具。"""
    _WORKSPACE[0] = Path(workspace).resolve()
    return [
        read_file, write_file, edit_file, list_dir,
        glob_files, grep_files, run_shell, todo_write, remember,
    ]


__all__ = ["build_builtin_tools"]

"""记忆系统。

Agent 的"记忆"要分成两层看，混在一起讲容易翻车：

  长期记忆（跨会话）
    - 项目约定：仓库里的 MAODING.md，人写、Agent 读、也可由 Agent 追加
    - 这类内容进系统提示，属于"每次都要知道"的常量

  短期记忆（会话内）
    - 对话历史：由 ContextManager 管，会随预算被折叠/裁剪
    - 会话快照：落盘到 .maodingcode/sessions/*.json，用于 --continue 恢复

为什么不把长期记忆做成向量库？因为"项目约定"是几十到几百行的结构化文本，
全量塞进系统提示比检索更准也更快。上向量检索是解决"大"，不是解决"准"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .message import Message

MAX_MEMORY_CHARS = 8_000  # 超过就截断，避免把系统提示撑爆


@dataclass
class MemoryStore:
    """长期记忆 + 会话快照。"""

    workspace_root: Path
    memory_filename: str = "MAODING.md"
    state_dirname: str = ".maodingcode"

    _buffer: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 长期记忆
    @property
    def memory_path(self) -> Path:
        return self.workspace_root / self.memory_filename

    def load_project_memory(self) -> str:
        """读取项目约定文件，供系统提示注入。"""
        path = self.memory_path
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return ""
        if len(text) > MAX_MEMORY_CHARS:
            text = text[:MAX_MEMORY_CHARS] + "\n\n[... 项目约定过长已截断 ...]"
        return text

    def ensure_project_memory(self, template: str | None = None) -> Path:
        """首次运行时生成一个空的约定文件，引导用户填写。"""
        path = self.memory_path
        if not path.exists():
            path.write_text(template or DEFAULT_MEMORY_TEMPLATE, encoding="utf-8")
        return path

    def append_note(self, note: str, section: str = "临时记录") -> None:
        """把一条结论追加到项目约定，让下一次会话也能"记得"。

        只追加、不重写，避免模型把人工整理过的内容覆盖掉。
        """
        path = self.ensure_project_memory()
        stamp = time.strftime("%Y-%m-%d %H:%M")
        block = f"\n### [{stamp}] {section}\n\n{note.strip()}\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)

    # ---------------------------------------------------------------- 会话快照
    @property
    def session_dir(self) -> Path:
        return self.workspace_root / self.state_dirname / "sessions"

    def save_session(self, name: str, messages: list[Message], extra: dict | None = None) -> Path:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_dir / f"{_safe_name(name)}.json"
        payload = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message_count": len(messages),
            "meta": extra or {},
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tool_calls": m.tool_calls,
                    "tool_call_id": m.tool_call_id,
                    "name": m.name,
                }
                for m in messages
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_session(self, name: str) -> list[Message]:
        path = self.session_dir / f"{_safe_name(name)}.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        out: list[Message] = []
        for item in payload.get("messages", []):
            out.append(
                Message(
                    role=item.get("role", "user"),
                    content=item.get("content", ""),
                    tool_calls=item.get("tool_calls") or [],
                    tool_call_id=item.get("tool_call_id", ""),
                    name=item.get("name", ""),
                )
            )
        return out

    def latest_session(self) -> str | None:
        if not self.session_dir.is_dir():
            return None
        files = sorted(self.session_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return files[-1].stem if files else None


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return cleaned[:64] or "session"


DEFAULT_MEMORY_TEMPLATE = """# MAODING.md

> 这个文件是给 Agent 看的项目约定，每次会话都会读进系统提示。
> 只写"必须一直记得"的东西，越长越贵。

## 项目概览
- 这个项目是做什么的：
- 技术栈：
- 代码在哪个目录：

## 构建与测试
```bash
# 安装依赖
# 运行测试
```

## 约定
- 命名 / 目录结构约定：
- 提交信息格式：

## 已知坑
- 
"""


__all__ = ["MemoryStore", "DEFAULT_MEMORY_TEMPLATE", "MAX_MEMORY_CHARS"]

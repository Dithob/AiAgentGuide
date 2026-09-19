"""Skills —— 把"做事的方法"从代码里挪到文档里。

工具和技能的区别，想清楚这一点整个设计就顺了：

    工具（Tool）  = 能做什么        —— 由代码实现，模型只能调用
    技能（Skill） = 该怎么做        —— 由 Markdown 描述，模型读了照做

所以技能本质上是一种**按需加载的提示词包**。它的价值在于：

  - 改行为不用改代码、不用重新部署，用户自己就能加；
  - 只在相关的时候才进上下文——否则 20 个技能全塞进系统提示会直接拖垮效果；
  - 天然可组合：技能里可以引用别的技能，可以要求调用某些工具。

加载流程：
    discover() 扫描目录 -> 解析 frontmatter 取 name/description（只读头部）
    -> 只把「名字 + 一句话说明」放进系统提示（这份索引很便宜）
    -> 模型判断相关时调用 use_skill(name)，此时才把正文读进上下文
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..security.policy import Reversibility
from ..tools.base import Tool

SKILL_FILENAME = "SKILL.md"
MAX_SKILL_BODY_CHARS = 20_000
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    # frontmatter 里的其余字段，例如 allowed_tools / when_to_use
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def source_dir(self) -> Path:
        return self.path.parent

    def index_line(self) -> str:
        return f"- {self.name}：{self.description}"

    def render(self) -> str:
        """注入上下文时的呈现形式。带上路径，方便技能正文里引用同目录的脚本。"""
        tail = ""
        if len(self.body) > MAX_SKILL_BODY_CHARS:
            tail = "\n\n[技能内容过长已截断]"
            body = self.body[:MAX_SKILL_BODY_CHARS]
        else:
            body = self.body
        return (
            f"# 技能：{self.name}\n\n"
            f"技能目录：{self.source_dir}\n"
            f"（该目录下的脚本与模板可用相对路径直接引用）\n\n"
            f"{body}{tail}"
        )


class SkillLoader:
    def __init__(self, workspace_root: Path, skill_dirs: tuple[str, ...] = ()) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.skill_dirs = skill_dirs or (".maodingcode/skills", "skills")
        self.skills: dict[str, Skill] = {}

    # ------------------------------------------------------------------ 发现
    def discover(self) -> list[Skill]:
        self.skills.clear()
        for d in self.skill_dirs:
            root = Path(d)
            if not root.is_absolute():
                root = self.workspace_root / root
            if not root.is_dir():
                continue
            self._scan(root)
        return list(self.skills.values())

    def _scan(self, root: Path) -> None:
        # 支持两种布局：
        #   skills/<name>/SKILL.md      —— 带资源目录的技能（推荐）
        #   skills/<name>/<name>.md     —— 单文件技能
        for entry in sorted(root.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                candidate = entry / SKILL_FILENAME
                if candidate.is_file():
                    self._load_file(candidate, fallback_name=entry.name)
            elif entry.suffix.lower() == ".md":
                if entry.name.lower() == SKILL_FILENAME.lower():
                    self._load_file(entry, fallback_name=root.name)
                else:
                    self._load_file(entry, fallback_name=entry.stem)

    def _load_file(self, path: Path, fallback_name: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or fallback_name).strip()
        if not name or name in self.skills:
            return
        description = str(meta.get("description") or _first_paragraph(body)).strip()
        self.skills[name] = Skill(
            name=name,
            description=description[:400],
            body=body.strip(),
            path=path,
            meta=meta,
        )

    # ------------------------------------------------------------------ 输出
    def index_for_prompt(self, max_chars: int = 6_000) -> str:
        """只把「名称 + 说明」放进系统提示。

        这是技能机制能规模化的关键：加 100 个技能，系统提示也只涨几 KB。
        """
        if not self.skills:
            return ""
        lines = [
            "## 可用技能（Skills）",
            "",
            "下面是按需加载的做事方法。当某个技能的适用场景与用户当前诉求吻合时，",
            "调用 use_skill 把它的完整内容读进上下文，再照着做。不要凭技能名猜测内容。",
            "",
        ]
        used = 0
        for skill in sorted(self.skills.values(), key=lambda s: s.name):
            line = skill.index_line()
            if used + len(line) > max_chars:
                lines.append(f"... 还有 {len(self.skills) - len(lines) + 4} 个技能未列出")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        lowered = name.strip().lower()
        for key, skill in self.skills.items():
            if key.lower() == lowered:
                return skill
        return None

    def describe(self) -> str:
        if not self.skills:
            dirs = ", ".join(str(d) for d in self.skill_dirs)
            return f"未发现技能（搜索目录：{dirs}）"
        rows = [f"已加载 {len(self.skills)} 个技能：", ""]
        for skill in sorted(self.skills.values(), key=lambda s: s.name):
            rows.append(f"  {skill.name}")
            rows.append(f"      {skill.description}")
            rows.append(f"      路径：{skill.path}")
        return "\n".join(rows)

    def as_tools(self) -> list[Tool]:
        """把技能暴露成两个工具：查目录、加载正文。"""
        return [_use_skill_tool(self), _list_skills_tool(self)]


# ------------------------------------------------------------------ frontmatter
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter。

    只支持 `key: value` 与简单的 `key: [a, b]`，刻意不引入 PyYAML——
    技能头部就这么几个字段，多一个依赖不值得。
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
            meta[key.strip()] = [v for v in items if v]
        else:
            meta[key.strip()] = value
    return meta, text[match.end() :]


def _first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        cleaned = " ".join(block.split())
        if cleaned and not cleaned.startswith("#"):
            return cleaned[:200]
    return "(无说明)"


# ------------------------------------------------------------------ 工具包装
def _use_skill_tool(loader: SkillLoader) -> Tool:
    def handler(name: str) -> str:
        skill = loader.get(name)
        if skill is None:
            available = ", ".join(sorted(loader.skills)) or "（无）"
            return f"没有名为 {name!r} 的技能。可用技能：{available}"
        return skill.render()

    return Tool(
        name="use_skill",
        description=(
            "加载某个技能的完整内容到上下文。\n"
            "什么时候用：当前任务与系统提示里列出的某个技能描述吻合时。\n"
            "加载后请严格按技能里写的步骤执行。"
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能名"}},
            "required": ["name"],
        },
        handler=handler,
        read_only=True,
        reversibility=Reversibility.READ_ONLY,
        source="skill",
    )


def _list_skills_tool(loader: SkillLoader) -> Tool:
    def handler() -> str:
        return loader.describe()

    return Tool(
        name="list_skills",
        description="列出所有可用技能的完整清单（含路径）。用于确认技能是否已安装。",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
        read_only=True,
        reversibility=Reversibility.READ_ONLY,
        source="skill",
    )


__all__ = ["Skill", "SkillLoader", "parse_frontmatter", "SKILL_FILENAME"]

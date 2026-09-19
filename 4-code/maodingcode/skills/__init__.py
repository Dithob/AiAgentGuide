"""技能系统：按需加载的提示词包。"""

from .loader import Skill, SkillLoader, parse_frontmatter

__all__ = ["Skill", "SkillLoader", "parse_frontmatter"]

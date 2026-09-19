"""会话装配。

把"怎么把各个部件拼起来"从"怎么把结果显示给用户"里拆出来。
CLI、RPC 服务、编辑器插件三种宿主共用这一份装配，
差别只在传入的 `emit`（事件怎么展示）和 `confirm`（权限怎么问）。

之前的版本把装配写在 cli.py 里，导致想加一个 RPC 入口就得复制一遍装配逻辑——
装配顺序一旦分叉，权限回填、技能索引这些隐性依赖迟早对不上。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import AppConfig
from .core.context import ContextManager, default_system_sections
from .core.loop import AgentLoop, ConfirmFn, EventSink, LoopResult
from .core.memory import MemoryStore
from .core.observability import Tracer
from .mcp.client import MCPManager
from .provider import build_provider
from .security.policy import Policy
from .skills.loader import SkillLoader
from .tools.builtin import build_builtin_tools
from .tools.registry import ToolRegistry


class Session:
    """一次会话的全部状态与组件。"""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.workspace: Path = cfg.security.workspace_root

        # 1) 权限策略必须先于工具注册建好——注册时会回调它写入可逆性
        self.policy = Policy(config=cfg.security)
        self.tracer = Tracer(
            price_in_per_1k=cfg.provider.price_in_per_1k,
            price_out_per_1k=cfg.provider.price_out_per_1k,
        )
        self.registry = ToolRegistry(policy=self.policy, tracer=self.tracer)

        # 2) 内置工具
        self.registry.register_all(build_builtin_tools(self.workspace))

        # 3) 技能：只把索引写进系统提示，正文按需加载
        self.skills = SkillLoader(self.workspace, cfg.skill_dirs)
        self.skills.discover()
        self.registry.register_all(self.skills.as_tools())

        # 4) MCP：单个 Server 失败不影响启动
        self.mcp = MCPManager.from_configs(cfg.mcp_servers)
        self.registry.register_all(self.mcp.tools())

        # 5) 记忆 + 上下文
        self.memory = MemoryStore(workspace_root=self.workspace)
        self.context = ContextManager(
            workspace_root=self.workspace,
            max_context_tokens=cfg.provider.context_window,
            keep_recent_turns=cfg.keep_recent_turns,
        )

        # 6) 模型
        self.provider = build_provider(cfg.provider)

        self.refresh_system_prompt()

    # ------------------------------------------------------------------ 提示词
    def refresh_system_prompt(self) -> None:
        """重建系统提示。改了记忆或技能之后要调用，否则这一轮读到的还是旧的。"""
        sections = default_system_sections(self.workspace)
        memory = self.memory.load_project_memory()
        if memory:
            sections["memory"] = f"## 项目约定（来自 MAODING.md）\n\n{memory}"
        skills_index = self.skills.index_for_prompt()
        if skills_index:
            sections["skills"] = skills_index
        self.context.set_base_sections(sections)

    # ------------------------------------------------------------------ 执行
    def make_loop(self, emit: EventSink, confirm: ConfirmFn | None = None) -> AgentLoop:
        return AgentLoop(
            provider=self.provider,
            context=self.context,
            registry=self.registry,
            policy=self.policy,
            tracer=self.tracer,
            max_steps=self.cfg.max_steps,
            emit=emit,
            stream=self.cfg.stream,
            confirm=confirm,
        )

    def run_turn(
        self,
        prompt: str,
        emit: EventSink,
        confirm: ConfirmFn | None = None,
        loop: AgentLoop | None = None,
    ) -> tuple[LoopResult, dict[str, Any]]:
        """跑一轮，返回结果与本次统计。统计每轮重置。"""
        self.tracer.reset()
        active = loop or self.make_loop(emit, confirm)
        result = active.run(prompt)
        return result, self.tracer.summary()

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self.mcp.close()
        self.provider.close()

    # ------------------------------------------------------------------ 自省
    def describe_components(self) -> dict[str, str]:
        return {
            "tools": self.registry.describe(),
            "skills": self.skills.describe(),
            "mcp": self.mcp.describe(),
            "policy": self.policy.describe(),
        }


__all__ = ["Session", "Callable"]

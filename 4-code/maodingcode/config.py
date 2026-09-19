"""分层配置。

优先级（后者覆盖前者）：
    内置默认值  <  环境变量 / .env  <  项目配置文件 maodingcode.toml  <  命令行参数

这样设计的理由：
  - .env 放密钥，不进版本库；
  - maodingcode.toml 放项目级约定（权限模式、技能目录、MCP 服务），可以进版本库；
  - 命令行参数用于单次调试覆盖。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

CONFIG_FILENAME = "maodingcode.toml"
MEMORY_FILENAME = "MAODING.md"
STATE_DIRNAME = ".maodingcode"

# 权限模式，从松到紧
PERMISSION_MODES = ("auto", "suggest", "readonly")


@dataclass(slots=True)
class ProviderConfig:
    """模型接入配置。任何兼容 OpenAI Chat Completions 的服务都能接。"""

    name: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: float = 120.0
    # 是否启用流式（SSE）输出。真正生效还需要调用方传入 on_delta 回调，
    # 见 OpenAICompatProvider.chat()：没有消费方就不该付流式的复杂度。
    stream: bool = True
    # 上下文窗口大小，用于预算计算；不追求精确，够做裁剪决策即可
    context_window: int = 128_000
    # 千 token 单价（输入/输出），用于成本估算；None 表示不估算
    price_in_per_1k: float | None = None
    price_out_per_1k: float | None = None


@dataclass(slots=True)
class SecurityConfig:
    """权限沙箱配置。"""

    workspace_root: Path = field(default_factory=Path.cwd)
    # auto=只读工具直接过，写类工具直接执行（危险，仅本地可信目录用）
    # suggest=写类/命令类工具需要确认（默认）
    # readonly=只允许只读工具
    permission_mode: str = "suggest"
    # 追加的黑名单，命中的命令一律拒绝
    command_denylist: tuple[str, ...] = ()
    # 允许写入的额外目录（默认只允许 workspace_root 内）
    writable_extra: tuple[Path, ...] = ()
    # 单条命令超时（秒）
    shell_timeout: float = 60.0


@dataclass(slots=True)
class MCPServerConfig:
    """一个 MCP Server 的连接配置。"""

    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # 走 SSE / HTTP 时使用
    enabled: bool = True

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


@dataclass(slots=True)
class AppConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    # 技能搜索目录（相对 workspace 或绝对路径）
    skill_dirs: tuple[str, ...] = (".maodingcode/skills", "skills")
    max_steps: int = 25
    # 保留最近 N 轮完整消息，更早的工具输出会被折叠
    keep_recent_turns: int = 6
    stream: bool = True
    debug: bool = False

    @property
    def memory_path(self) -> Path:
        return self.security.workspace_root / MEMORY_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.security.workspace_root / STATE_DIRNAME


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：不依赖 python-dotenv，减少一个运行时依赖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        # 已存在的真实环境变量优先，不覆盖
        os.environ.setdefault(key, value)


def _find_config_file(start: Path) -> Path | None:
    """从 start 向上找 maodingcode.toml，找不到返回 None。"""
    cur = start.resolve()
    for _ in range(64):
        candidate = cur / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _as_path_tuple(values: Any, base: Path) -> tuple[Path, ...]:
    if not values:
        return ()
    out = []
    for v in values:
        p = Path(str(v))
        out.append(p if p.is_absolute() else (base / p))
    return tuple(out)


def _parse_mcp_servers(raw: Any) -> list[MCPServerConfig]:
    if not raw:
        return []
    if isinstance(raw, dict):
        # 也允许 {"name": {...}} 形式
        raw = [dict(cfg, name=name) for name, cfg in raw.items()]
    servers: list[MCPServerConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"MCP 配置项必须是对象，收到：{type(item).__name__}")
        servers.append(
            MCPServerConfig(
                name=str(item.get("name", "")),
                command=str(item.get("command", "")),
                args=tuple(str(a) for a in item.get("args", ())),
                env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                url=str(item.get("url", "")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return servers


def load_config(
    workspace: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """装配配置。

    Args:
        workspace: 工作区根目录，默认当前目录。
        overrides: 命令行级覆盖，键名与 AppConfig 字段一致。

    Raises:
        ConfigError: 权限模式非法，或配置文件格式错误。
    """
    overrides = dict(overrides or {})
    root = Path(workspace or overrides.pop("workspace", None) or Path.cwd())
    root = root.resolve()

    _load_dotenv(root / ".env")
    workspace = root

    cfg = AppConfig()
    cfg.security.workspace_root = workspace

    # --- 1. 环境变量 ---
    env = os.environ
    if env.get("MOD_PROVIDER") or env.get("AI_PROVIDER"):
        cfg.provider.name = env.get("MOD_PROVIDER") or env["AI_PROVIDER"]
    if env.get("MOD_BASE_URL") or env.get("AI_BASE_URL"):
        cfg.provider.base_url = env.get("MOD_BASE_URL") or env["AI_BASE_URL"]
    if env.get("MOD_API_KEY") or env.get("AI_API_KEY"):
        cfg.provider.api_key = env.get("MOD_API_KEY") or env["AI_API_KEY"]
    if env.get("MOD_MODEL") or env.get("AI_MODEL_NAME"):
        cfg.provider.model = env.get("MOD_MODEL") or env["AI_MODEL_NAME"]
    if env.get("MOD_MAX_STEPS"):
        cfg.max_steps = int(env["MOD_MAX_STEPS"])
    if env.get("MOD_PERMISSION_MODE"):
        cfg.security.permission_mode = env["MOD_PERMISSION_MODE"]

    # --- 2. 项目配置文件 ---
    # 坑：不能写成 `Path(overrides.pop("config_path", "") or "") or _find_config_file(...)`。
    # Path("") 会变成 Path('.')，而 Path 没有定义 __bool__，所以它恒为真值，
    # 结果就永远短路到 Path('.')，配置文件一次都读不到。
    explicit_config = overrides.pop("config_path", None)
    config_path = Path(explicit_config) if explicit_config else _find_config_file(workspace)
    if config_path and Path(config_path).is_file():
        try:
            data = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"解析 {config_path} 失败：{exc}") from exc
        cfg = _apply_file_config(cfg, data, workspace)

    # --- 3. 命令行覆盖 ---
    cfg = _apply_overrides(cfg, overrides)

    # 流式开关由 agent 层统一持有，但真正读它的是 Provider，
    # 所以在装配末尾同步一次，避免两处各存一份、改了这头忘了那头。
    cfg.provider = replace(cfg.provider, stream=cfg.stream)

    if cfg.security.permission_mode not in PERMISSION_MODES:
        raise ConfigError(
            f"permission_mode 必须是 {PERMISSION_MODES} 之一，"
            f"收到 {cfg.security.permission_mode!r}"
        )
    if not cfg.provider.api_key:
        raise ConfigError(
            "缺少 API Key。请在 .env 中设置 AI_API_KEY，"
            "或在 environment 中设置 MOD_API_KEY。"
        )
    return cfg


def _apply_file_config(cfg: AppConfig, data: dict[str, Any], workspace: Path) -> AppConfig:
    model = data.get("model") or {}
    if model:
        cfg.provider = replace(
            cfg.provider,
            name=str(model.get("provider", cfg.provider.name)),
            base_url=str(model.get("base_url", cfg.provider.base_url)),
            api_key=str(model.get("api_key", cfg.provider.api_key)),
            model=str(model.get("name", cfg.provider.model)),
            temperature=float(model.get("temperature", cfg.provider.temperature)),
            max_tokens=int(model.get("max_tokens", cfg.provider.max_tokens)),
            context_window=int(model.get("context_window", cfg.provider.context_window)),
            price_in_per_1k=model.get("price_in_per_1k", cfg.provider.price_in_per_1k),
            price_out_per_1k=model.get("price_out_per_1k", cfg.provider.price_out_per_1k),
        )

    sec = data.get("security") or {}
    if sec:
        cfg.security = replace(
            cfg.security,
            permission_mode=str(sec.get("permission_mode", cfg.security.permission_mode)),
            command_denylist=tuple(
                str(x) for x in sec.get("command_denylist", cfg.security.command_denylist)
            ),
            writable_extra=_as_path_tuple(sec.get("writable_extra"), workspace),
            shell_timeout=float(sec.get("shell_timeout", cfg.security.shell_timeout)),
        )

    agent = data.get("agent") or {}
    if agent:
        cfg.max_steps = int(agent.get("max_steps", cfg.max_steps))
        cfg.keep_recent_turns = int(agent.get("keep_recent_turns", cfg.keep_recent_turns))
        cfg.stream = bool(agent.get("stream", cfg.stream))
        cfg.skill_dirs = tuple(str(x) for x in agent.get("skill_dirs", cfg.skill_dirs))

    servers = _parse_mcp_servers(data.get("mcp", {}).get("servers"))
    if servers:
        cfg.mcp_servers = servers
    return cfg


def _apply_overrides(cfg: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """把命令行参数合并进配置。

    关键规则：**只覆盖调用方真正传了值的字段。**

    这里踩过一个很隐蔽的坑：早期写法是按"键在不在集合里"来挑字段，
    于是 `--api-key` 没传时 `overrides["api_key"]` 是 `None`，照样被塞进
    `replace()`，把 `.env` 里读到的密钥覆盖成 None。现象是
    "明明配了 .env，却报缺少 API Key"——极难排查，因为配置看着是对的。

    所以判据必须是**值**而不是**键**：None 和空串都表示"没传"。
    例外是 `stream`，它的 False 是有效值，所以单独用 `in` 判断。
    """
    provider_keys = {}
    for key in ("base_url", "api_key", "temperature", "max_tokens", "context_window"):
        value = overrides.get(key)
        if value is not None and value != "":
            provider_keys[key] = value
    if overrides.get("model"):
        provider_keys["model"] = overrides["model"]
    if provider_keys:
        cfg.provider = replace(cfg.provider, **provider_keys)

    if overrides.get("permission_mode"):
        cfg.security = replace(cfg.security, permission_mode=overrides["permission_mode"])
    if overrides.get("max_steps"):
        cfg.max_steps = int(overrides["max_steps"])
    if overrides.get("debug"):
        cfg.debug = True
    if "stream" in overrides:
        cfg.stream = bool(overrides["stream"])
    return cfg


def config_as_dict(cfg: AppConfig) -> dict[str, Any]:
    """脱敏后的配置快照，供 /config 命令与日志使用。"""
    key = cfg.provider.api_key
    masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else ("***" if key else "")
    return {
        "model": {
            "provider": cfg.provider.name,
            "base_url": cfg.provider.base_url,
            "api_key": masked,
            "name": cfg.provider.model,
            "context_window": cfg.provider.context_window,
        },
        "security": {
            "workspace_root": str(cfg.security.workspace_root),
            "permission_mode": cfg.security.permission_mode,
            "command_denylist": list(cfg.security.command_denylist),
        },
        "agent": {"max_steps": cfg.max_steps, "keep_recent_turns": cfg.keep_recent_turns},
        "mcp_servers": [s.name for s in cfg.mcp_servers if s.enabled],
        "skill_dirs": list(cfg.skill_dirs),
    }


def dump_config(cfg: AppConfig) -> str:
    return json.dumps(config_as_dict(cfg), ensure_ascii=False, indent=2)

"""工具抽象。

一个工具要能被模型正确调用，必须提供三样东西：
    1. name        —— 唯一标识，模型按名字调用
    2. description —— 决定模型"什么时候想起来用它"，比参数说明重要得多
    3. parameters  —— JSON Schema，决定模型能不能把参数写对

另外两个内部字段决定它怎么被执行：
    handler        —— 真正的 Python 函数
    read_only      —— 只读工具可以直接跑，写类工具要走权限确认
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from ..security.policy import Reversibility


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    read_only: bool = False
    reversibility: Reversibility | None = None
    source: str = "builtin"  # builtin / mcp:<server> / skill:<name>
    # 参数名 -> 简介，用于生成给用户看的确认提示
    arg_hints: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- 执行
    def call(self, **kwargs: Any) -> str:
        """执行并统一把返回值转成字符串。

        工具约定：**永远返回字符串**。返回 dict/对象会逼着每个调用点
        自己序列化，而且容易漏掉 ensure_ascii，中文变转义序列。
        """
        result = self.handler(**kwargs)
        if result is None:
            return "(无输出)"
        if isinstance(result, str):
            return result
        import json

        try:
            return json.dumps(result, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(result)

    # ---------------------------------------------------------------- 校验
    def validate_args(self, args: dict[str, Any]) -> str | None:
        """轻量参数校验：只查必填和类型，返回错误文案或 None。

        不做完整的 JSON Schema 校验——把模糊的 schema 错误直接回灌给模型，
        让它自己修，通常比在本地拦下来更省事。
        """
        schema = self.parameters or {}
        required = schema.get("required") or []
        props = schema.get("properties") or {}

        missing = [r for r in required if r not in args or args[r] in (None, "")]
        if missing:
            return f"缺少必填参数：{', '.join(missing)}"

        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in args.items():
            spec = props.get(key)
            if not spec:
                continue
            expected = type_map.get(spec.get("type", ""))
            if expected and not isinstance(value, expected):
                return (
                    f"参数 {key} 类型应为 {spec.get('type')}，"
                    f"实际收到 {type(value).__name__}"
                )
            if "enum" in spec and value not in spec["enum"]:
                return f"参数 {key} 必须是 {spec['enum']} 之一，收到 {value!r}"
        return None

    # ---------------------------------------------------------------- 描述
    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "required": []},
            },
        }


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    read_only: bool = False,
    reversibility: Reversibility | None = None,
    source: str = "builtin",
) -> Callable[[Callable[..., Any]], Tool]:
    """装饰器写法，让工具定义和函数贴在一起，读代码时不用来回跳。"""

    def wrap(fn: Callable[..., Any]) -> Tool:
        sig = inspect.signature(fn)
        hints = {p: "" for p in sig.parameters}
        return Tool(
            name=name,
            description=description.strip(),
            parameters=parameters,
            handler=fn,
            read_only=read_only,
            reversibility=reversibility,
            source=source,
            arg_hints=hints,
        )

    return wrap


__all__ = ["Tool", "tool"]

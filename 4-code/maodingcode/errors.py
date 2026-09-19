"""统一异常体系。

分层原则：越靠近用户越"可解释"，越靠近内核越"可判断"。

    MaodingError                 所有异常的基类
    ├── ConfigError              配置缺失/非法
    ├── ProviderError            模型服务调用失败（可重试 / 不可重试）
    ├── ToolError                工具执行失败
    │   └── ToolNotFoundError
    ├── PermissionDenied         策略层拒绝
    └── MCPError                 外部 MCP Server 通信失败
"""

from __future__ import annotations


class MaodingError(Exception):
    """项目内所有异常的基类。"""


class ConfigError(MaodingError):
    """配置缺失或非法。"""


class ProviderError(MaodingError):
    """模型服务调用失败。

    retryable 用来区分「网络抖动」与「参数写错了」——
    前者适合指数退避重试，后者重试一百次也没用。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolError(MaodingError):
    """工具执行失败。会被转成文本回灌给模型，让它自己纠偏。"""


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class PermissionDenied(MaodingError):
    """被策略层拒绝。"""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class MCPError(MaodingError):
    """MCP Server 通信失败。"""

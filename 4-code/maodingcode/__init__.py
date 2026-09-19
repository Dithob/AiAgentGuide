"""MaoDingCode —— 本地运行的 AI 编码助手。

一个可读、可改、可讲的 coding agent 参考实现：
    Agent Loop 引擎 -> 工具系统 -> 权限沙箱 -> MCP / Skills 扩展

设计说明见 docs/ 目录。核心只有一条链路：

    user_input -> context 装配 -> provider.chat -> tool_calls?
        -> policy 校验 -> tool 执行 -> 结果回灌 -> 回到 provider.chat
        -> 无 tool_calls -> 输出最终回答
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

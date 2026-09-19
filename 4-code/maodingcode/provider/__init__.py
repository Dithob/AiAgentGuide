"""模型接入层。

对外只暴露两个概念：

    Provider.chat(messages, tools) -> ChatResponse

之所以收成一个窄接口，是因为 Agent Loop 不该关心你用的是哪家模型。
换模型 = 换一个 Provider 实例，Loop 一行都不用改。
"""

from .base import ChatResponse, Provider, ToolCallRequest, Usage
from .openai_compat import OpenAICompatProvider, build_provider

__all__ = [
    "ChatResponse",
    "Provider",
    "ToolCallRequest",
    "Usage",
    "OpenAICompatProvider",
    "build_provider",
]

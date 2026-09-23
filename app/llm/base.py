"""LLM 抽象层:只走 DeepSeek API。settings 的 llm.provider = deepseek | off,模型在 llm.model。
用于:对话(工具调用)、周报文案、长期记忆维护、中文标题翻译。核心链路(定时/推送/入库)不依赖 LLM。"""

from .. import events, store
from . import deepseek


async def chat(messages: list[dict], tools: list[dict] | None = None, timeout: int = 45,
               model: str | None = None, response_format: dict | None = None) -> dict | None:
    """多轮 + 工具调用:返回 assistant message(content / tool_calls / reasoning_content);
    provider=off、未配置或出错时返回 None(调用方必须有无 LLM 的降级路径)。"""
    if store.get("llm.provider", "deepseek") == "off":
        return None
    try:
        msg = await deepseek.chat(messages, tools, timeout=timeout, model=model,
                                  response_format=response_format)
        events.log("llm_call", {"provider": "deepseek",
                                "prompt_head": str(messages[-1].get("content"))[:80],
                                "tools": [c["function"]["name"] for c in msg.get("tool_calls") or []]})
        return msg
    except Exception as e:  # noqa: BLE001
        events.log("llm_error", {"provider": "deepseek", "error": str(e)[:300]})
        return None


async def complete(prompt: str, system: str = "", timeout: int = 45) -> str | None:
    """单轮纯文本(周报/记忆维护/翻译)。"""
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    msg = await chat(messages, timeout=timeout)
    return msg.get("content") if msg else None

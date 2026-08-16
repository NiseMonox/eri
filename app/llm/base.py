"""LLM 抽象层:provider 在 settings(llm.provider)里切,claude(订阅内 claude -p)| deepseek | off。
只用于两处:自然语言解析兜底、周报文案。核心链路(定时/推送/入库)不依赖 LLM。"""

from .. import events, store


async def complete(prompt: str, system: str = "", timeout: int = 45,
                   purpose: str = "parse") -> str | None:
    """返回文本;provider=off、未配置或出错时返回 None(调用方必须有无 LLM 的降级路径)。
    purpose ∈ parse(默认,解析/对话决策)| report(周报文案),各自可在 settings 配不同模型。"""
    provider = store.get("llm.provider", "claude")
    if provider == "off":
        return None
    try:
        if provider == "deepseek":
            from . import deepseek

            text = await deepseek.complete(prompt, system, timeout=timeout)
        else:
            from . import claude_cli

            text = await claude_cli.complete(prompt, system, timeout=timeout, purpose=purpose)
        events.log("llm_call", {"provider": provider, "prompt_head": prompt[:80],
                                "ok": text is not None})
        return text
    except Exception as e:  # noqa: BLE001
        events.log("llm_error", {"provider": provider, "error": str(e)[:300]})
        return None

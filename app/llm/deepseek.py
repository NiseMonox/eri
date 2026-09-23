"""DeepSeek API(OpenAI 兼容)。模型取 settings 的 llm.model,默认 deepseek-flash(V4.1 Flash,带推理)。"""

import httpx

from .. import store
from ..config import settings

URL = "https://api.deepseek.com/chat/completions"


async def chat(messages: list[dict], tools: list[dict] | None = None, timeout: int = 45) -> dict:
    """返回 choices[0].message(content / tool_calls / reasoning_content)。"""
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    body = {"model": store.get("llm.model", "deepseek-flash"), "messages": messages, "temperature": 0.3}
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            URL,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json=body,
        )
        if r.status_code != 200:
            # 余额不足(402)/key 失效(401)/模型名错 的原因只在响应体里,raise_for_status 会丢掉
            raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]

"""DeepSeek API 备用 provider(OpenAI 兼容)。"""

import httpx

from ..config import settings

URL = "https://api.deepseek.com/chat/completions"


async def complete(prompt: str, system: str = "", timeout: int = 45) -> str | None:
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            URL,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={"model": "deepseek-chat", "messages": messages, "temperature": 0.3},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

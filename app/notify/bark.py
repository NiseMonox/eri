"""Bark 推送:LXC 内 bark-server → APNs → iPhone。level=critical 需在 Bark App 内授权重要警报。"""

import httpx

from ..config import settings


async def send(
    title: str,
    body: str,
    *,
    level: str = "active",          # passive|active|timeSensitive|critical
    url: str | None = None,
    call: int | None = None,        # 1 = 循环响铃(闹钟用)
    volume: int | None = None,      # 0-10,仅 critical 有效
    sound: str | None = None,
    group: str = "health-hub",
) -> bool:
    if not settings.bark_device_key:
        return False
    payload: dict = {
        "device_key": settings.bark_device_key,
        "title": title,
        "body": body,
        "level": level,
        "group": group,
    }
    if url:
        payload["url"] = url
    if call is not None:
        payload["call"] = str(call)
    if volume is not None:
        payload["volume"] = volume
    if sound:
        payload["sound"] = sound

    async with httpx.AsyncClient(timeout=5) as client:
        for attempt in (1, 2):
            try:
                r = await client.post(f"{settings.bark_url}/push", json=payload)
                r.raise_for_status()
                return True
            except httpx.HTTPError:
                if attempt == 2:
                    raise
    return False

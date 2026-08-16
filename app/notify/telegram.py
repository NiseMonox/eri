"""Telegram 纯发送封装:直接打 Bot API,不依赖 PTB 运行状态(Phase 2 的 bot 复用)。"""

import httpx

from ..config import settings


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def send_message(text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
    """buttons: [(label, callback_data)],单列 inline 键盘。"""
    if not configured():
        return False
    payload: dict = {"chat_id": settings.telegram_chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]
        }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(_api("sendMessage"), json=payload)
        r.raise_for_status()
    return True


async def send_photo(png: bytes, caption: str = "") -> bool:
    if not configured():
        return False
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            _api("sendPhoto"),
            data={"chat_id": settings.telegram_chat_id, "caption": caption},
            files={"photo": ("chart.png", png, "image/png")},
        )
        r.raise_for_status()
    return True


async def edit_message_text(message_id: int, text: str) -> bool:
    if not configured():
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            _api("editMessageText"),
            json={"chat_id": settings.telegram_chat_id, "message_id": message_id, "text": text},
        )
        r.raise_for_status()
    return True

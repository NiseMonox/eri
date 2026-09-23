"""通知统一入口。三档 profile 固化 Bark 参数;失败 fail-soft(写 event_log,不抛给调度层)。"""

from .. import emoji, events
from . import bark, telegram

PROFILES: dict[str, dict] = {
    "alarm": {"level": "critical", "call": 1, "volume": 10},
    "med": {"level": "timeSensitive"},
    "info": {"level": "active"},
}


async def notify(
    profile: str,
    title: str,
    body: str,
    *,
    url: str | None = None,
    channel: str = "both",          # both|bark|telegram
    tg_buttons: list[tuple[str, str]] | None = None,
    photo: bytes | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> dict:
    # 标题里的例行名/提醒名、周报的 LLM 文案都可能带 emoji:发出去之前统一删
    title, body = emoji.strip(title), emoji.strip(body)
    if tg_buttons:
        tg_buttons = [(emoji.strip(label), data) for label, data in tg_buttons]
    bark_kwargs = dict(PROFILES.get(profile, PROFILES["info"]))
    sent = {"bark": False, "telegram": False}

    if channel in ("both", "bark"):
        try:
            sent["bark"] = await bark.send(title, body, url=url, **bark_kwargs)
        except Exception as e:  # noqa: BLE001 — 通知失败不能压垮调度
            events.log("notify_error", {"channel": "bark", "error": str(e), "title": title},
                       ref_type, ref_id)

    if channel in ("both", "telegram"):
        try:
            if photo is not None:
                sent["telegram"] = await telegram.send_photo(photo, caption=f"{title}\n{body}".strip())
            else:
                text = f"{title}\n{body}".strip()
                if url and not tg_buttons:
                    text += f"\n{url}"
                sent["telegram"] = await telegram.send_message(text, buttons=tg_buttons)
        except Exception as e:  # noqa: BLE001
            events.log("notify_error", {"channel": "telegram", "error": str(e), "title": title},
                       ref_type, ref_id)

    if sent["bark"] or sent["telegram"]:
        events.log("notify_sent", {"profile": profile, "title": title, "sent": sent}, ref_type, ref_id)
    return sent

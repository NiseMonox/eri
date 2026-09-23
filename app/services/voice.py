"""语音对话的一轮:音频 → eri-stt 识别 → conversation.handle(voice=True) → 家里音箱念回复。
HTTP 接口(/api/voice/turn,Windows / iOS 客户端)和 Telegram 语音消息共用。接口约定见 docs/voice/eri-voice-api.md。

- 回复在家里音箱念,静音时段也照常念(是用户主动说的);speak=False(人不在家)时家里完全不出声
- 响应先返回、后台再念;等 LLM 时不持锁,两台设备可以同时说,回复按完成先后排队念(tts 里串行)
- 识别失败的那一轮不进对话历史"""

import re
import time

from .. import bg, events, store
from ..audio import stt, tts
from ..audio.manager import audio_manager
from . import conversation

FAIL_REPLY = {
    "empty": "ごめん、うまく聞き取れなかった。もう一回言って?",
    "too_long": "ちょっと長すぎたみたい。{max_sec}秒以内で話してね",
    "bad_audio": "音声ファイルがうまく読めなかったよ",
    "stt_unavailable": "ごめん、いま耳の調子が悪いみたい。文字で送ってくれる?",
    "internal": "ごめん、うまくいかなかった。もう一回試してね",
}
MORE_ON_SCREEN = "続きは画面を見てね"
_RE_BULLET = re.compile(r"^\s*[・\-*•]\s*")


def max_sec() -> int:
    return int(store.get("voice.max_sec", 60) or 60)


async def turn(audio: bytes, *, source: str, filename: str = "audio", content_type: str | None = None,
               speak: bool = True, trigger: str = "ptt") -> dict:
    """返回 {"ok", "heard", "reply", "photo", "language", "spoken", "error"}。
    source 进 chat_log 的 via(voice-<source>);trigger=wake(将来的唤醒词)时没听清就不出声。"""
    t0 = time.monotonic()
    will_speak = speak and tts.enabled()
    info = {"src": source, "trigger": trigger, "bytes": len(audio)}
    try:
        r = await stt.transcribe(audio, filename=filename, content_type=content_type, max_sec=max_sec())
    except stt.SttRejected as e:
        return _fail(e.reason, info, t0, will_speak and trigger != "wake")
    except stt.SttUnavailable:
        return _fail("stt_unavailable", info, t0, will_speak and trigger != "wake")
    heard = (r.get("text") or "").strip()
    info.update(duration=r.get("duration"), language=r.get("language"), segments=r.get("segments"),
                stt_ms=_ms(t0))
    if not heard:
        return _fail("empty", info, t0, will_speak and trigger != "wake", language=r.get("language"))

    t1 = time.monotonic()
    try:
        res = await conversation.handle(heard, via=f"voice-{source}", voice=True, reply_spoken=will_speak)
    except Exception as e:  # noqa: BLE001 — 一轮出错不能让客户端等到超时
        events.log("voice_error", {"src": source, "error": f"{type(e).__name__}: {e}"[:200]})
        return _fail("internal", info, t0, will_speak and trigger != "wake")
    # 停了闹钟:早安播报(时刻+今日待办)已经在念,就不再念回复
    speak_reply = will_speak and not res.get("briefing")
    after = res.get("after_speech") or []
    if speak_reply or after:
        bg.spawn(_speak_then(for_speech(res["reply"]) if speak_reply else None, after),
                 name=f"voice-speak-{source}")
    events.log("voice_turn", {**info, "llm_ms": _ms(t1), "total_ms": _ms(t0), "ok": res["ok"],
                              "chars": len(res["reply"]), "heard": heard[:40], "spoken": speak_reply,
                              "briefing": bool(res.get("briefing"))})
    return {"ok": res["ok"], "heard": heard, "reply": res["reply"], "photo": res.get("photo"),
            "language": r.get("language"), "spoken": speak_reply, "error": None}


def _fail(code: str, info: dict, t0: float, speak_it: bool, language: str | None = None) -> dict:
    reply = FAIL_REPLY[code].format(max_sec=max_sec())
    if speak_it:
        bg.spawn(_speak_then(reply, []), name="voice-speak-fail")
    events.log("voice_turn", {**info, "total_ms": _ms(t0), "ok": False, "error": code})
    return {"ok": False, "heard": "", "reply": reply, "photo": None, "language": language,
            "spoken": speak_it, "error": code}


async def _speak_then(text: str | None, after: list[dict]) -> None:
    """念回复(静音时段也念:用户主动说的),念完再开始放延后的音频(白噪音)。"""
    if text:
        ok = await tts.announce(text, force=True, translate=False)
        events.log("voice_spoke", {"ok": ok, "chars": len(text)})
    for payload in after:
        try:
            await audio_manager.start(payload)
        except Exception as e:  # noqa: BLE001
            events.log("audio_error", {"at": "after_speech", "error": str(e)[:200]})


def for_speech(text: str) -> str:
    """念给人听的版本:「・」列表和换行摊平成一句;太长就在句号处截断,提示看屏幕(完整版在设备上)。"""
    out = ""
    for line in text.splitlines():
        line = _RE_BULLET.sub("", line).strip()
        if not line:
            continue
        if out and out[-1] not in "。!！?？、:：":
            out += "、"
        out += line
    limit = int(store.get("voice.speak_max_chars", 120) or 120)
    if len(out) <= limit:
        return out
    cut = max(out.rfind(p, 0, limit) for p in "。!！?？")
    head = out[:cut + 1] if cut >= limit // 3 else out[:limit] + "…。"
    return head + MORE_ON_SCREEN


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)

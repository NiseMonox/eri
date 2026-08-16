"""TTS 播报层:VOICEVOX(ずんだもん)合成 + 缓存 + 静音时段 + 中文标题 LLM 转日语。
全链 fail-soft:任何失败只写 event_log,绝不影响 Bark/核心提醒。"""

import asyncio
import hashlib
import json
import re
from datetime import time as dtime
from pathlib import Path

import httpx

from .. import clock, events, store
from ..config import settings

CACHE_DIR = settings.db_path.parent / "tts-cache"
TRANSLATION_CACHE = CACHE_DIR / "translations.json"

RE_KANA = re.compile(r"[ぁ-んァ-ヶー]")
RE_CJK = re.compile(r"[一-鿿]")

# 播报串行:防止两次播报交错时,前一次的「恢复音乐」掐掉后一次;顺带串行化翻译缓存读写
_announce_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(store.get("tts.enabled", True))


def quiet_now() -> bool:
    """tts.quiet 形如 "23:00-08:00",支持跨午夜。空串=永不静音。"""
    q = store.get("tts.quiet", "23:00-08:00") or ""
    if "-" not in q:
        return False
    try:
        s, e = q.split("-", 1)
        sh, sm = map(int, s.strip().split(":"))
        eh, em = map(int, e.strip().split(":"))
    except ValueError:
        return False
    start, end = dtime(sh, sm), dtime(eh, em)
    now = clock.now_local().time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _load_translations() -> dict:
    try:
        return json.loads(TRANSLATION_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_translation(orig: str, ja: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    d = _load_translations()
    d[orig] = ja
    TRANSLATION_CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=1))


async def ensure_ja(text: str) -> str:
    """含假名→原样;无假名但含汉字→LLM 归一化(中文翻成日语,已是日语原样返回),
    每个文本只处理一次、永久缓存。LLM 不可用时原文硬念(音读凑合能懂)。
    注:纯靠简体特征字表会漏掉「去健身房」这类全部由日语也有的汉字组成的中文,故全量归一化。"""
    t = text.strip()
    if not t or RE_KANA.search(t) or not RE_CJK.search(t):
        return t
    cached = _load_translations().get(t)
    if cached:
        return cached
    from ..llm import base as llm

    ja = await llm.complete(
        "下面是一个提醒事项标题,可能是中文也可能是日语汉字词。"
        "如果是中文,翻成简短自然的日语;如果已经是日语,原样返回。"
        f"只输出结果,不要引号不要解释:{t}",
        timeout=30,
    )
    if ja:
        ja = ja.strip().strip("「」\"'")
        _save_translation(t, ja)
        return ja
    return t


def _cache_path(ja_text: str, speaker: int) -> Path:
    key = hashlib.sha1(f"{speaker}|{ja_text}".encode()).hexdigest()[:24]
    return CACHE_DIR / f"{key}.wav"


async def synth(ja_text: str) -> Path:
    """VOICEVOX 合成(带缓存,2 次重试)。失败抛异常,由 announce 兜底。"""
    speaker = int(store.get("tts.speaker", 3) or 3)
    engine = (store.get("tts.engine_url", "http://127.0.0.1:50021") or "").rstrip("/")
    path = _cache_path(ja_text, speaker)
    if path.is_file() and path.stat().st_size > 0:
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for _ in range(2):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                q = await client.post(f"{engine}/audio_query",
                                      params={"text": ja_text, "speaker": speaker})
                q.raise_for_status()
                wav = await client.post(f"{engine}/synthesis", params={"speaker": speaker},
                                        json=q.json())
                wav.raise_for_status()
            path.write_bytes(wav.content)
            return path
        except Exception as e:  # noqa: BLE001
            last_err = e
            await asyncio.sleep(0.5)
    raise RuntimeError(f"VOICEVOX 合成失败: {last_err}")


async def announce(text: str, *, force: bool = False) -> bool:
    """播报一段(会自动转日语)。enabled/quiet 判定在此;force=True 跳过静音(闹钟报时用)。"""
    if not enabled():
        return False
    if not force and quiet_now():
        return False
    async with _announce_lock:
        try:
            ja = await ensure_ja(text)
            path = await synth(ja)
        except Exception as e:  # noqa: BLE001
            events.log("tts_error", {"error": str(e)[:200], "text": text[:60]})
            return False
        from .manager import audio_manager

        return await audio_manager.announce_file(str(path))


# --- 固定句式 ---

async def announce_med(med_name: str) -> bool:
    name = await ensure_ja(med_name)
    return await announce(f"お薬の時間だよ。{name}、忘れないでね")


async def announce_weight() -> bool:
    return await announce("体重を測ろう")


async def announce_reminder(title: str) -> bool:
    t = await ensure_ja(title)
    return await announce(f"リマインダーだよ。{t}")


def _speak_replies() -> bool:
    return bool(store.get("tts.speak_replies", True))


async def announce_snoozed(title: str, until_local) -> bool:
    """对话确认:推迟。tts.speak_replies=false 时不出声(只走文字渠道)。"""
    if not _speak_replies():
        return False
    t = await ensure_ja(title)
    return await announce(f"はーい。{until_local.strftime('%H時%M分')}に、また{t}の声かけるね")


async def announce_done(title: str) -> bool:
    if not _speak_replies():
        return False
    t = await ensure_ja(title)
    return await announce(f"えらい!{t}、完了だよ")


async def announce_nag(title: str, nth: int) -> bool:
    t = await ensure_ja(title)
    if nth >= 2:
        return await announce(f"もう{nth}回目だよ!{t}、まだやってないの?")
    return await announce(f"{t}、忘れてない?")


async def announce_morning() -> bool:
    """闹钟停止后的报时+今日待办。不受静音时段限制。"""
    from ..services import reminders

    now = clock.now_local()
    agenda = reminders.today_agenda()
    text = f"おはよう。{now.hour}時{now.minute}分だよ。"
    if agenda:
        heads = [await ensure_ja(a["title"]) for a in agenda[:3]]
        text += f"今日の予定は{len(agenda)}件。" + "、".join(heads) + "、だよ。"
    else:
        text += "今日の予定は特にないよ。ゆっくりしてね。"
    return await announce(text, force=True)


async def engine_alive() -> bool:
    engine = (store.get("tts.engine_url", "http://127.0.0.1:50021") or "").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{engine}/version")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def prewarm() -> None:
    """启动时预合成固定句式,首次播报零等待。引擎不在就算了。"""
    if not await engine_alive():
        return
    for t in ("体重を測ろう", "お薬の時間だよ", "リマインダーだよ"):
        try:
            await synth(t)
        except Exception:  # noqa: BLE001
            return


def trim_cache(keep: int = 200) -> int:
    files = sorted(CACHE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    n = 0
    for f in files[keep:]:
        f.unlink(missing_ok=True)
        n += 1
    return n

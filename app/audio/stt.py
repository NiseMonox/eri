"""语音识别客户端:调本机的 eri-stt(stt/server.py,OpenAI 兼容的 /v1/audio/transcriptions)。
每次都是用户主动说话、连不上会立刻失败,所以不做熔断(不像 embed.py);错误限频记 stt_error。"""

import time

import httpx

from .. import events, store


class SttUnavailable(RuntimeError):
    """识别服务没在跑 / 超时 / 5xx。"""


class SttRejected(ValueError):
    """识别服务拒收:reason = too_long | bad_audio。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


TIMEOUT = httpx.Timeout(30, connect=2)
_last_err_log = 0.0        # stt_error 事件限频:10 分钟一条


def _url() -> str:
    return str(store.get("voice.stt_url", "http://127.0.0.1:8310") or "").rstrip("/")


async def _post(files: dict, data: dict) -> httpx.Response:
    """测试替身的接缝。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.post(f"{_url()}/v1/audio/transcriptions", files=files, data=data)


def _log_error(msg: str) -> None:
    global _last_err_log
    if time.monotonic() - _last_err_log > 600:
        _last_err_log = time.monotonic()
        events.log("stt_error", {"error": msg[:200]})


async def transcribe(audio: bytes, *, filename: str = "audio.wav", content_type: str | None = None,
                     max_sec: float | None = None) -> dict:
    """返回 {"text", "language", "duration", "segments", "elapsed_ms", "reason"?};text 为空 = 没听到人声。
    连不上/超时/5xx → SttUnavailable;超长 → SttRejected("too_long");解不开 → SttRejected("bad_audio")。"""
    files = {"file": (filename or "audio", audio, content_type or "application/octet-stream")}
    data = {"max_sec": str(max_sec)} if max_sec else {}
    try:
        r = await _post(files, data)
    except httpx.HTTPError as e:
        _log_error(f"{type(e).__name__}: {e}")
        raise SttUnavailable(str(e) or type(e).__name__) from e
    if r.status_code == 413:
        raise SttRejected("too_long")
    if r.status_code == 400:
        raise SttRejected("bad_audio")
    if r.status_code != 200:
        _log_error(f"HTTP {r.status_code}: {r.text[:150]}")
        raise SttUnavailable(f"HTTP {r.status_code}")
    return r.json()


async def alive() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{_url()}/health")
        return r.status_code == 200 and bool(r.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def reset() -> None:
    """测试用:清限频状态。"""
    global _last_err_log
    _last_err_log = 0.0

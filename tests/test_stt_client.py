"""STT 客户端(app/audio/stt.py)的错误映射:连不上/5xx → SttUnavailable,413/400 → SttRejected。"""

import httpx
import pytest

from app import events
from app.audio import stt


def _respond(monkeypatch, status: int, body: dict | None = None):
    sent = []

    async def post(files, data):
        sent.append((files, data))
        return httpx.Response(status, json=body or {})

    monkeypatch.setattr(stt, "_post", post)
    return sent


async def test_ok_passes_json_and_max_sec(fresh_db, monkeypatch):
    sent = _respond(monkeypatch, 200, {"text": "こんにちは", "language": "ja"})
    r = await stt.transcribe(b"RIFF", filename="u.wav", content_type="audio/wav", max_sec=60)
    assert r["text"] == "こんにちは"
    files, data = sent[0]
    assert files["file"][0] == "u.wav" and files["file"][2] == "audio/wav" and data == {"max_sec": "60"}


@pytest.mark.parametrize("status,reason", [(413, "too_long"), (400, "bad_audio")])
async def test_rejections(fresh_db, monkeypatch, status, reason):
    _respond(monkeypatch, status, {"detail": reason})
    with pytest.raises(stt.SttRejected) as e:
        await stt.transcribe(b"x")
    assert e.value.reason == reason


async def test_server_error_and_unreachable_are_unavailable(fresh_db, monkeypatch):
    _respond(monkeypatch, 503, {"detail": "model not loaded"})
    with pytest.raises(stt.SttUnavailable):
        await stt.transcribe(b"x")
    assert len(events.recent(kind_prefix="stt_error")) == 1

    async def refused(files, data):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(stt, "_post", refused)
    for _ in range(3):
        with pytest.raises(stt.SttUnavailable):
            await stt.transcribe(b"x")
    assert len(events.recent(kind_prefix="stt_error")) == 1      # 限频:10 分钟内不再多记

"""Telegram 语音消息(bot/handlers.on_voice):时长两种写法、超长不下载、转发的不听、回复带上听到的原话。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import store
from app.bot import handlers
from app.config import settings
from app.services import voice


class FakeMsg:
    def __init__(self, *, duration=3, file_size=20_000, forwarded=False, audio=False):
        media = SimpleNamespace(duration=duration, file_size=file_size, mime_type="audio/ogg",
                                get_file=self._get_file)
        self.voice, self.audio = (None, media) if audio else (media, None)
        self.forward_origin = object() if forwarded else None
        self.downloads = 0
        self.texts: list[str] = []
        self.photos: list[tuple] = []

    async def _get_file(self):
        async def download():
            self.downloads += 1
            return bytearray(b"OggS" + b"\0" * 50)
        return SimpleNamespace(download_as_bytearray=download)

    async def reply_text(self, text):
        self.texts.append(text)

    async def reply_photo(self, photo, caption=None):
        self.photos.append((photo, caption))

    async def reply_chat_action(self, action):
        pass


def _update(msg, chat_id="42"):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_message=msg)


@pytest.fixture()
def turns(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    calls = []
    result = {"ok": True, "heard": "今何時?", "reply": "10時だよ", "photo": None, "language": "ja",
              "spoken": True, "error": None}

    async def fake_turn(audio, **kw):
        calls.append({"bytes": len(audio), **kw})
        return dict(result)

    monkeypatch.setattr(voice, "turn", fake_turn)
    return SimpleNamespace(calls=calls, result=result)


@pytest.mark.parametrize("duration", [3, timedelta(seconds=3)])
async def test_voice_message_round_trip(turns, duration):
    msg = FakeMsg(duration=duration)
    await handlers.on_voice(_update(msg), None)
    assert msg.texts == ["「今何時?」\n\n10時だよ"]
    c = turns.calls[0]
    assert (c["source"], c["speak"], c["filename"], c["content_type"], c["bytes"]) == \
        ("tg", True, "voice.ogg", "audio/ogg", 54)


@pytest.mark.parametrize("duration", [61, timedelta(seconds=90)])
async def test_too_long_is_refused_without_download(turns, duration):
    msg = FakeMsg(duration=duration)
    await handlers.on_voice(_update(msg), None)
    assert msg.texts == ["長すぎるよ(60秒まで)"] and msg.downloads == 0 and turns.calls == []


async def test_ignores_strangers_and_forwarded_voice(turns):
    msg = FakeMsg()
    await handlers.on_voice(_update(msg, chat_id="999"), None)
    assert msg.texts == [] and turns.calls == []
    fwd = FakeMsg(forwarded=True)
    await handlers.on_voice(_update(fwd), None)
    assert "転送" in fwd.texts[0] and fwd.downloads == 0 and turns.calls == []


async def test_speak_setting_and_photo(turns):
    store.set("voice.speak_telegram", False)
    turns.result.update(photo=b"PNG", reply="グラフだよ")
    msg = FakeMsg(audio=True)
    await handlers.on_voice(_update(msg), None)
    assert turns.calls[0]["speak"] is False and turns.calls[0]["filename"] == "audio"
    assert msg.photos == [(b"PNG", "「今何時?」\n\nグラフだよ")]


async def test_nothing_heard_shows_reply_only(turns):
    turns.result.update(heard="", reply="ごめん、うまく聞き取れなかった。もう一回言って?", ok=False, error="empty")
    msg = FakeMsg()
    await handlers.on_voice(_update(msg), None)
    assert msg.texts == ["ごめん、うまく聞き取れなかった。もう一回言って?"]

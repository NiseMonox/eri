"""语音入口:services/voice.turn(识别 → 对话 → 家里音箱念回复)和 /api/voice/* 接口。
约定见 docs/voice/eri-voice-api.md。STT / LLM / TTS 全部是假的。"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import bg, clock, db, store
from app.audio import stt, tts
from app.audio.manager import audio_manager as am
from app.config import settings
from app.services import conversation, reminders, voice, weights

T0 = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)     # JST 10:00


@pytest.fixture()
def heard(monkeypatch):
    """假识别:heard.text = 这次「听到」的话;记下收到的参数。"""
    class Heard:
        text = "今何時?"
        language = "ja"
        calls: list = []

    async def transcribe(audio, *, filename="audio", content_type=None, max_sec=None):
        Heard.calls.append({"bytes": len(audio), "filename": filename, "max_sec": max_sec})
        return {"text": Heard.text, "language": Heard.language, "duration": 1.5, "segments": 1}

    Heard.calls = []
    monkeypatch.setattr(stt, "transcribe", transcribe)
    return Heard


@pytest.fixture()
def spoken(monkeypatch):
    """记下家里音箱念了什么(tts.announce),以及工具自己的播报确认(announce_done / announce_snoozed)。"""
    calls = {"announce": [], "done": [], "snoozed": [], "morning": []}

    async def announce(text, *, force=False, translate=True):
        calls["announce"].append({"text": text, "force": force, "translate": translate})
        return True

    async def done(title):
        calls["done"].append(title)
        return True

    async def snoozed(title, until_local):
        calls["snoozed"].append(title)
        return True

    async def morning():
        calls["morning"].append(1)
        return True

    monkeypatch.setattr(tts, "announce", announce)
    monkeypatch.setattr(tts, "announce_done", done)
    monkeypatch.setattr(tts, "announce_snoozed", snoozed)
    monkeypatch.setattr(tts, "announce_morning", morning)
    return calls


async def _turn(**kw):
    kw.setdefault("source", "pc")
    res = await voice.turn(b"RIFF....", **kw)
    await bg.drain(1)          # 回复是在后台念的
    return res


def _chat_vias():
    return [r["via"] for r in db.get_db().execute("SELECT via FROM chat_log ORDER BY id")]


async def test_normal_turn_speaks_reply_once(fresh_db, heard, spoken, fake_llm):
    clock.set_override(T0)
    seen = fake_llm("10時だよ")
    res = await _turn()
    assert res == {"ok": True, "heard": "今何時?", "reply": "10時だよ", "photo": None, "language": "ja",
                   "spoken": True, "error": None}
    assert spoken["announce"] == [{"text": "10時だよ", "force": True, "translate": False}]
    assert _chat_vias() == ["voice-pc", "voice-pc"]
    assert seen[0][-1]["content"].endswith(conversation.VOICE_NOTE)
    assert heard.calls[0]["max_sec"] == 60


async def test_voice_turn_does_not_double_speak_confirmations(fresh_db, heard, spoken, fake_llm):
    """语音轮只念最后的回复:mark_done 不再单独播「えらい!…完了だよ」;文字渠道照旧。"""
    clock.set_override(T0)
    inst = reminders.create_instance("倒垃圾", schedule_id=7)
    reminders.mark_notified(inst["id"])
    heard.text = "ゴミ出し終わった"
    fake_llm([("mark_done", {"reminder_id": inst["id"]})], "おつかれさま!")
    await _turn()
    assert spoken["done"] == [] and [c["text"] for c in spoken["announce"]] == ["おつかれさま!"]

    inst2 = reminders.create_instance("倒垃圾", schedule_id=8)
    reminders.mark_notified(inst2["id"])
    fake_llm([("mark_done", {"reminder_id": inst2["id"]})], "えらい!")
    await conversation.handle("ゴミ出し終わった", via="telegram")
    assert spoken["done"] == ["倒垃圾"]


async def test_regex_fallback_in_voice_turn(fresh_db, heard, spoken):
    """LLM 挂了:「62.5キロ。」(识别会带句号)照样记体重,模板回复只念一次;来源记成 voice-ios。"""
    clock.set_override(T0)
    heard.text = "62.5キロ。"
    res = await _turn(source="ios")
    assert res["ok"] and "62.5" in res["reply"]
    assert len(spoken["announce"]) == 1 and spoken["done"] == []
    assert weights.recent_days(1)[0]["source"] == "voice-ios"


async def test_voice_note_only_for_spoken_channels(fresh_db, fake_llm):
    clock.set_override(T0)
    seen = fake_llm("はい", "はい", "はい")
    await conversation.handle("テスト", via="voice-pc", voice=True)
    await conversation.handle("テスト", via="siri")
    await conversation.handle("テスト", via="telegram")
    assert [s[-1]["content"].endswith(conversation.VOICE_NOTE) for s in seen] == [True, True, False]


async def test_speak_false_and_tts_disabled_stay_silent(fresh_db, heard, spoken, fake_llm):
    clock.set_override(T0)
    fake_llm("はい")
    assert (await _turn(speak=False))["spoken"] is False
    store.set("tts.enabled", False)
    fake_llm("はい")
    assert (await _turn())["spoken"] is False
    assert spoken["announce"] == []


async def test_quiet_hours_still_speak(fresh_db, heard, spoken, fake_llm):
    """深夜的静音时段也照常念(用户主动说的):force=True。"""
    clock.set_override(datetime(2026, 8, 15, 15, 30, tzinfo=timezone.utc))   # JST 00:30
    assert tts.quiet_now()
    fake_llm("おやすみ")
    await _turn()
    assert spoken["announce"][0]["force"] is True


async def test_nothing_heard(fresh_db, heard, spoken):
    heard.text = ""
    res = await _turn()
    assert res["error"] == "empty" and res["heard"] == "" and res["spoken"] is True
    assert spoken["announce"][0]["text"] == voice.FAIL_REPLY["empty"]
    res = await _turn(trigger="wake")          # 将来的唤醒词:没听清就不出声
    assert res["spoken"] is False and len(spoken["announce"]) == 1
    assert _chat_vias() == []                  # 识别失败不进对话历史


async def test_stt_failures(fresh_db, spoken, monkeypatch):
    res = await _turn()                        # conftest 默认:识别服务连不上
    assert res["error"] == "stt_unavailable" and not res["ok"]

    async def too_long(*a, **k):
        raise stt.SttRejected("too_long")

    monkeypatch.setattr(stt, "transcribe", too_long)
    res = await _turn(speak=False)
    assert res["error"] == "too_long" and "60秒" in res["reply"] and res["spoken"] is False


async def test_alarm_stop_briefing_replaces_reply(fresh_db, heard, spoken, fake_llm):
    """停了闹钟:早安播报(时刻+今日待办)代替回复念出来,回复文字照样返回。"""
    await am.start({"action": "alarm", "target_volume": 80})
    heard.text = "起きた、止めて"
    fake_llm([("control_audio", {"op": "stop"})], "おはよう!")
    res = await _turn()
    assert res["reply"] == "おはよう!" and res["spoken"] is False
    assert spoken["morning"] == [1] and spoken["announce"] == []


async def test_white_noise_starts_after_the_reply(fresh_db, heard, spoken, fake_llm):
    heard.text = "白噪音开一个小时"
    fake_llm([("control_audio", {"op": "play_noise", "duration_min": 60})], "流すね、おやすみ")
    res = await _turn()
    assert res["spoken"] is True and [c["text"] for c in spoken["announce"]] == ["流すね、おやすみ"]
    assert am.status()["action"] == "white_noise"          # 念完才开始放
    await am.stop(reason="test")


def test_for_speech(fresh_db):
    assert voice.for_speech("今日の予定:\n・08:00 お薬\n・21:00 ストレッチ") == "今日の予定:08:00 お薬、21:00 ストレッチ"
    assert voice.for_speech("はい。\nおやすみ") == "はい。おやすみ"
    store.set("voice.speak_max_chars", 20)
    out = voice.for_speech("一つ目の文はここで終わるよ。二つ目の文はとても長くて画面で見てほしい内容だよ。")
    assert out == "一つ目の文はここで終わるよ。" + voice.MORE_ON_SCREEN


# --- HTTP 接口 ---

@pytest.fixture()
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "t0ken")
    from app.main import app

    return TestClient(app)     # 不用 with:不跑 lifespan


@pytest.fixture()
def turn_calls(monkeypatch):
    calls = []

    async def fake_turn(audio, **kw):
        calls.append({"bytes": len(audio), **kw})
        return {"ok": True, "heard": "今何時?", "reply": "10時だよ", "photo": None, "language": "ja",
                "spoken": kw["speak"], "error": None}

    monkeypatch.setattr(voice, "turn", fake_turn)
    return calls


def test_api_turn_parses_form(client, turn_calls):
    r = client.post("/api/voice/turn", headers={"X-Token": "t0ken"},
                    files={"file": ("u.wav", b"RIFF" + b"\0" * 100, "audio/wav")},
                    data={"device": "PC", "speak": "0", "trigger": "bogus"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "heard": "今何時?", "reply": "10時だよ", "spoken": False,
                        "language": "ja", "error": None, "has_photo": False}
    c = turn_calls[0]
    assert (c["source"], c["speak"], c["trigger"], c["filename"], c["bytes"]) == ("pc", False, "ptt", "u.wav", 104)
    client.post("/api/voice/turn", headers={"X-Token": "t0ken"},
                files={"file": ("u.wav", b"RIFF")}, data={"device": "../etc", "trigger": "intent"})
    assert (turn_calls[1]["source"], turn_calls[1]["speak"], turn_calls[1]["trigger"]) == ("app", True, "intent")


def test_api_turn_auth_before_upload(client, turn_calls):
    r = client.post("/api/voice/turn", files={"file": ("u.wav", b"RIFF")})
    assert r.status_code == 401 and turn_calls == []
    r = client.post("/api/voice/turn", headers={"X-Token": "wrong"}, files={"file": ("u.wav", b"RIFF")})
    assert r.status_code == 401 and turn_calls == []


def test_api_turn_rejects_missing_file_and_oversize(client, turn_calls, monkeypatch):
    r = client.post("/api/voice/turn", headers={"X-Token": "t0ken"}, data={"device": "pc"})
    assert r.status_code == 400
    from app.routers import voice as voice_router

    monkeypatch.setattr(voice_router, "MAX_BODY", 100)
    r = client.post("/api/voice/turn", headers={"X-Token": "t0ken"}, files={"file": ("u.wav", b"x" * 1000)})
    assert r.status_code == 413 and turn_calls == []


def test_api_ping(client, monkeypatch):
    assert client.get("/api/voice/ping").status_code == 401
    assert client.get("/api/voice/ping", headers={"X-Token": "t0ken"}).json() == \
        {"ok": True, "stt": False, "tts": False}

    async def up():
        return True

    monkeypatch.setattr(stt, "alive", up)
    monkeypatch.setattr(tts, "engine_alive", up)
    assert client.get("/api/voice/ping", headers={"X-Token": "t0ken"}).json() == \
        {"ok": True, "stt": True, "tts": True}


def test_weights_today_includes_voice_entries(client):
    """语音记的体重也要同步进 Apple 健康;Withings/HAE 的由官方 App 写,不重复给。"""
    weights.add_weight(62.5, clock.now_iso(), source="voice-ios")
    weights.add_weight(62.4, clock.now_iso(), source="withings")
    got = client.get("/api/weights/today", headers={"X-Token": "t0ken"}).json()
    assert [w["kg"] for w in got] == [62.5]

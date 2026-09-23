"""音频控制工具 control_audio 和【スピーカー】状态行:停闹钟/白噪音、放白噪音、调音量、调艾莉的声音。"""

import asyncio
from datetime import datetime, timezone

import pytest

from app import clock, store
from app.audio import tts
from app.audio.manager import audio_manager as am
from app.services import conversation
from app.services.conversation import TurnCtx

T0 = datetime(2026, 8, 15, 13, 0, tzinfo=timezone.utc)     # JST 22:00
NOISE = {"action": "white_noise", "preset": "brown", "volume": 40, "duration_min": 60}
ALARM = {"action": "alarm", "target_volume": 80, "max_min": 30}


@pytest.fixture()
async def briefings(monkeypatch):
    calls = []

    async def morning():
        calls.append(1)
        return True

    monkeypatch.setattr(tts, "announce_morning", morning)
    yield calls
    await am.stop(reason="test")


async def _audio(ctx: TurnCtx | None = None, **action):
    return await conversation._execute({"action": "audio", **action}, "", "telegram", ctx or TurnCtx())


async def test_speaker_line_states(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    assert conversation._speaker_line() == "【スピーカー】何も再生していない / エリの声 80"
    await am.start(NOISE)
    assert conversation._speaker_line() == \
        "【スピーカー】ホワイトノイズ(ブラウン)再生中・音量40・あと60分で自動停止 / エリの声 80"
    task = asyncio.create_task(am.announce_file("/tmp/say.wav"))
    await asyncio.sleep(0.01)
    assert conversation._speaker_line() == \
        "【スピーカー】エリが話し中(ホワイトノイズ(ブラウン)は一時停止中、音量40) / エリの声 80"
    fake_player.finish()
    await task
    assert "/media/" not in conversation._context() and "noise_brown" not in conversation._context()


async def test_stop_alarm_in_text_turn_triggers_morning_briefing(fresh_db, fake_player, briefings):
    await am.start(ALARM)
    ctx = TurnCtx()
    res = await _audio(ctx, op="stop")
    await asyncio.sleep(0)
    assert res["ok"] and "スピーカーで読み上げる" in res["reply"]
    assert am.status() == {"playing": False} and briefings == [1] and ctx.briefing


async def test_stop_alarm_in_voice_turn_when_away_is_quiet(fresh_db, fake_player, briefings):
    """语音轮但 speak=false(人不在家):安静地停,家里不播早安。"""
    await am.start(ALARM)
    ctx = TurnCtx(voice=True, reply_spoken=False)
    res = await _audio(ctx, op="stop")
    await asyncio.sleep(0)
    assert res["reply"] == "アラームを止めたよ" and briefings == [] and not ctx.briefing


async def test_stop_when_nothing_plays(fresh_db, fake_player, briefings):
    res = await _audio(op="stop")
    assert res["ok"] and res["reply"] == "いまは何も鳴ってないよ"


async def test_play_noise_in_text_turn_starts_now_and_clamps(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    res = await _audio(op="play_noise", preset="pink", volume=500, duration_min=9999)
    st = am.status()
    assert res["ok"] and st["action"] == "white_noise" and st["target_volume"] == 100
    assert st["label"] == "ホワイトノイズ(ピンク)"
    assert (clock.parse_iso(st["ends_at"]) - clock.now_utc()).total_seconds() == 480 * 60


async def test_play_noise_in_spoken_turn_waits_for_the_reply(fresh_db, fake_player, briefings):
    """回复要在音箱念:先停掉正在放的(闹钟也不触发早安),念完再放,免得「响—被打断—再响」。"""
    await am.start(ALARM)
    ctx = TurnCtx(voice=True, reply_spoken=True)
    res = await _audio(ctx, op="play_noise")
    await asyncio.sleep(0)
    assert res["ok"] and "返事のあとで" in res["reply"]
    assert am.status() == {"playing": False} and briefings == []
    assert ctx.after_speech == [{"action": "white_noise", "preset": "brown", "volume": 40, "duration_min": 60}]


async def test_set_volume(fresh_db, fake_player, briefings):
    assert not (await _audio(op="set_volume", volume=20))["ok"]        # 什么都没在放
    await am.start(NOISE)
    res = await _audio(op="set_volume", volume=25)
    assert res["ok"] and am.status()["target_volume"] == 25 and ("volume", 25) in fake_player.calls


async def test_set_voice_volume(fresh_db, fake_player, briefings):
    res = await _audio(op="set_voice_volume", volume=55)
    assert res["ok"] and store.get("tts.volume") == 55
    await _audio(op="set_voice_volume", volume=3)
    assert store.get("tts.volume") == 10                                # 最低 10,不能调成哑巴


async def test_stop_by_voice_when_llm_is_down(fresh_db, fake_player, briefings):
    """DeepSeek 挂了也能叫停:正则快路径认「止めて。」(识别结果会带句号)。"""
    await am.start(ALARM)
    res = await conversation.handle("止めて。", via="voice-pc", voice=True, reply_spoken=True)
    await asyncio.sleep(0)
    assert res["ok"] and res["briefing"] and am.status() == {"playing": False} and briefings == [1]


async def test_llm_tool_call_stops_white_noise(fresh_db, fake_player, briefings, fake_llm):
    await am.start(NOISE)
    seen = fake_llm([("control_audio", {"op": "stop"})], "止めたよ。おやすみ")
    res = await conversation.handle("白噪音关了吧", via="telegram")
    assert res["reply"] == "止めたよ。おやすみ" and am.status() == {"playing": False}
    assert "【スピーカー】ホワイトノイズ(ブラウン)再生中" in seen[0][-1]["content"]
    assert briefings == []                                              # 白噪音不是闹钟,不播早安

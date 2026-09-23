"""AudioManager 的「暂停 → 恢复」:播报打断白噪音/闹钟后从原处恢复——同一个停止 token(Bark 链接照样能停)、
同样的开始/结束时刻、当前音量;播报期间的 stop / set_volume 作用在被暂停的会话上。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import clock
from app.audio import tts
from app.audio.manager import audio_manager as am

T0 = datetime(2026, 8, 15, 13, 0, tzinfo=timezone.utc)     # JST 22:00
NOISE = {"action": "white_noise", "preset": "brown", "volume": 40, "duration_min": 60}
ALARM = {"action": "alarm", "target_volume": 80, "max_min": 30}


@pytest.fixture()
async def briefings(monkeypatch):
    """记录早安播报被触发了几次(停闹钟时)。"""
    calls = []

    async def morning():
        calls.append(clock.now_iso())
        return True

    monkeypatch.setattr(tts, "announce_morning", morning)
    yield calls
    await am.stop(reason="test")


async def _announce_in_background():
    task = asyncio.create_task(am.announce_file("/tmp/say.wav"))
    for _ in range(20):                     # 等它真的开始念(拿到锁、暂停会话、开始播放)
        await asyncio.sleep(0)
        if am.status().get("action") == "announce":
            break
    return task


async def test_resume_keeps_token_timer_and_volume(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    s = await am.start(NOISE)
    await am.set_volume(30)
    task = await _announce_in_background()
    st = am.status()
    assert st["action"] == "announce" and st["paused"] == "white_noise" and st["paused_volume"] == 30

    clock.set_override(T0 + timedelta(minutes=5))
    fake_player.finish()
    assert await task is True
    st = am.status()
    assert st["action"] == "white_noise" and st["target_volume"] == 30 and st["volume_now"] == 30
    assert st["started_at"] == s["started_at"] and st["ends_at"] == s["ends_at"]   # 计时不从头开始
    assert am.session["token"] == s["token"]                                       # Bark 的停止链接还有效
    assert fake_player.calls[-1] == ("play", s["source"], 30, True)


async def test_set_volume_during_announce_applies_on_resume(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    await am.start(NOISE)
    task = await _announce_in_background()
    assert await am.set_volume(15) is not None
    fake_player.finish()
    await task
    assert am.status()["target_volume"] == 15 and fake_player.calls[-1][2] == 15


async def test_stop_during_announce_cancels_resume_and_briefs_once(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    await am.start(ALARM)
    task = await _announce_in_background()
    assert await am.stop(reason="manual") is True
    fake_player.finish()
    await task
    await asyncio.sleep(0)
    assert am.status() == {"playing": False}          # 念完不再恢复闹钟
    assert len(briefings) == 1


async def test_no_resume_after_ends_at(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    await am.start({**NOISE, "duration_min": 1})
    task = await _announce_in_background()
    clock.set_override(T0 + timedelta(minutes=2))
    fake_player.finish()
    await task
    assert am.status() == {"playing": False}


async def test_old_token_still_stops_after_resume(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    s = await am.start(ALARM)
    task = await _announce_in_background()
    fake_player.finish()
    await task
    assert am.status()["action"] == "alarm"
    assert await am.stop(token=s["token"]) is True
    assert await am.stop(token="stale") is False


async def test_new_playback_during_announce_is_not_overridden(fresh_db, fake_player, briefings):
    """播报期间有人开了新的播放(比如定时闹钟到点):念完不再恢复旧的白噪音。"""
    clock.set_override(T0)
    await am.start(NOISE)
    task = await _announce_in_background()
    await am.start(ALARM)           # 和 mpv 一样:开始新的播放会结束正在念的那段
    await asyncio.wait_for(task, 1)
    assert am.status()["action"] == "alarm"


async def test_unfinished_fade_continues_from_current_volume(fresh_db, fake_player, briefings):
    clock.set_override(T0)
    await am.start({**ALARM, "fade_in_sec": 100})
    await asyncio.sleep(0)
    task = await _announce_in_background()
    paused_vol = am._paused["volume_now"]
    assert 0 < am._paused["fade_left"] <= 100
    fake_player.finish()
    await task
    plays = [c for c in fake_player.calls if c[0] == "play"]
    assert plays[-1] == ("play", am.session["source"], paused_vol, False)     # 从暂停时的音量接着放
    assert am._fade_task is not None and not am._fade_task.done()              # 渐强接着走

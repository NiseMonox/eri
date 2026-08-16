from datetime import datetime, timezone

import pytest

from app import clock
from app.audio import tts
from app.services import intents, routines


def _at_jst(h, m=0):
    # JST h:m == UTC (h-9):m
    clock.set_override(datetime(2026, 8, 15, (h - 9) % 24, m, tzinfo=timezone.utc))


def test_quiet_crossing_midnight(fresh_db):
    # 默认 23:00-08:00
    _at_jst(23, 30)
    assert tts.quiet_now() is True
    _at_jst(3)
    assert tts.quiet_now() is True
    _at_jst(7, 59)
    assert tts.quiet_now() is True
    _at_jst(8, 0)
    assert tts.quiet_now() is False
    _at_jst(12)
    assert tts.quiet_now() is False
    _at_jst(22, 59)
    assert tts.quiet_now() is False


def test_quiet_disabled(fresh_db):
    from app import store

    store.set("tts.quiet", "")
    _at_jst(3)
    assert tts.quiet_now() is False


def test_cache_path_deterministic(fresh_db):
    a = tts._cache_path("お薬の時間だよ", 3)
    b = tts._cache_path("お薬の時間だよ", 3)
    c = tts._cache_path("お薬の時間だよ", 1)
    assert a == b and a != c and a.suffix == ".wav"


async def test_ensure_ja_passthrough(fresh_db, monkeypatch, tmp_path):
    # 缓存指到 tmp,避免污染真实 data/tts-cache
    monkeypatch.setattr(tts, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tts, "TRANSLATION_CACHE", tmp_path / "translations.json")
    # 假名文本不动、不调 LLM
    called = []

    async def fake_llm(*a, **k):
        called.append(1)
        return "翻訳結果"

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", fake_llm)
    assert await tts.ensure_ja("お薬の時間だよ") == "お薬の時間だよ"
    assert await tts.ensure_ja("インターンシップ") == "インターンシップ"
    assert called == []
    # 简体中文 → 走 LLM 且缓存
    assert await tts.ensure_ja("出门买衣服") == "翻訳結果"
    assert await tts.ensure_ja("出门买衣服") == "翻訳結果"   # 第二次命中缓存
    assert len(called) == 1


async def test_intents_weight_and_med(fresh_db):
    r = await intents.execute({"intent": "weight", "weight_kg": 62.5, "measured_at": None},
                              via="siri")
    assert r["ok"] and "62.5" in r["reply"]
    # 同刻重复
    r2 = await intents.execute({"intent": "weight", "weight_kg": 62.5, "measured_at": None},
                               via="siri")
    assert r2["ok"] is False



async def test_routine_done_via_conversation(fresh_db, monkeypatch):
    """「薬飲んだ」正则快路径 → 最近的 med 类 routine 实例 done。"""
    from app.services import conversation
    from app.audio import tts as _tts

    async def no_tts(*a, **k):
        return False

    monkeypatch.setattr(_tts, "announce_done", no_tts)
    r3 = await conversation.handle("薬飲んだ", via="siri")
    assert "確認待ち" in r3["reply"]        # 还没有实例
    rt = routines.create("测试药", "med")
    inst = routines.create_instance(rt, None, force=True)
    r4 = await conversation.handle("薬飲んだ", via="siri")
    assert r4["ok"] and "完了" in r4["reply"]
    assert routines.instances(status="done")[0]["done_via"] == "siri"
    assert routines.instances(status="done")[0]["id"] == inst["id"]


async def test_intents_bad_llm_fields(fresh_db):
    r = await intents.execute({"intent": "weight"}, via="siri")
    assert r["ok"] is False and "読み取れなかった" in r["reply"]
    r2 = await intents.execute({"intent": "reminder", "title": "x", "due_at": "乱七八糟"},
                               via="siri")
    assert r2["ok"] is False

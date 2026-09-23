"""每日整理:抽取 → 向量配对 → 对账 → 一个事务写入。水位线、失败告警、护栏、冲突、撤销、积压、降级。
LLM 与向量全部是假的(fake_embedder + 按调用顺序回放的 JSON)。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import clock, db, store
from app.llm import base as llm
from app.services import consolidate, conversation, memories

NIGHT = datetime(2026, 9, 24, 4, 30, tzinfo=clock.TOKYO)    # 9/24 04:30:整理 9/23 那个习惯日


def _at(dt: datetime) -> None:
    clock.set_override(dt.astimezone(timezone.utc))


def _chat(when: str, user: str, reply: str = "うん", actions: list | None = None) -> None:
    ts = clock.iso(datetime.fromisoformat(when).replace(tzinfo=clock.TOKYO))
    conversation._log("telegram", "user", user, ts=ts)
    conversation._log("telegram", "assistant", reply, ts=ts, actions=actions)


def _script(monkeypatch, *steps) -> list:
    """按调用顺序回放夜间整理的 LLM:dict → JSON 文本、str 原样、None = 调用失败。记下 (阶段, 用户消息)。"""
    calls: list = []
    it = iter(steps)

    async def fake_chat(messages, tools=None, timeout=45, **kw):
        calls.append(("reconcile" if "逐条判定" in messages[0]["content"] else "extract", messages[1]["content"]))
        step = next(it)
        if step is None:
            return None
        return {"role": "assistant",
                "content": step if isinstance(step, str) else json.dumps(step, ensure_ascii=False)}

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


async def test_consolidates_yesterday_into_library(fresh_db, monkeypatch, fake_embedder):
    _at(datetime(2026, 9, 22, 12, 0, tzinfo=clock.TOKYO))
    gym = memories.add("preference", "通っているジムはサクラフィット", core=True, source="user")
    peanut = memories.add("fact", "ピーナッツアレルギーがある", source="user")
    interview = memories.add("schedule", "9/30 に面接がある", event_at=clock.parse_flexible_jst("2026-09-30"),
                             source="user")
    await memories.ensure_vectors()
    _chat("2026-09-23T10:00", "ジムをゴールドジムに変えたよ")
    _chat("2026-09-23T21:00", "9/30 の面接は14時からだって",
          actions=[{"tool": "record_weight", "ok": True, "result": "70.1kg 記録したよ"}])
    _chat("2026-09-24T05:00", "おはよう")                   # 今天的习惯日:今晚才整理
    monkeypatch.setattr(consolidate, "NEIGHBOR_SIM", 0.3)
    assert float(fake_embedder("9/30 14:00 に面接がある") @ fake_embedder(interview["text"])) > 0.3
    calls = _script(
        monkeypatch,
        {"memories": [
            {"kind": "preference", "text": "通っているジムはゴールドジム", "event_at": None},
            {"kind": "schedule", "text": "9/30 14:00 に面接がある", "event_at": "2026-09-30 14:00"},
            {"kind": "fact", "text": "ピーナッツアレルギーがある", "event_at": None},
            {"kind": "event", "text": "明日は雨らしい", "event_at": "2026-09-23"},        # 相对时间词 → 拒收
        ], "diary": "9/23 はジムを変えた話をして、面接の時間が決まった。"},
        {"decisions": [
            {"c": 0, "op": "SUPERSEDE", "id": gym["id"]},
            {"c": 1, "op": "UPDATE", "id": interview["id"], "text": "9/30 14:00 に面接がある"},
            {"c": 2, "op": "SKIP", "id": peanut["id"]},
        ]})
    _at(NIGHT)
    [day] = (await consolidate.run())["days"]
    assert day["day"] == "2026-09-23"
    assert (day["added"], day["updated"], day["superseded"], day["skipped"], day["rejected"]) == (0, 1, 1, 1, 1)
    assert day["diary"] is True
    [new_gym] = memories.list_rows(q="ゴールドジム")
    old_gym = memories.get(gym["id"])
    assert old_gym["active"] == 0 and old_gym["superseded_by"] == new_gym["id"] and old_gym["valid_until"]
    assert new_gym["core"] == 1 and new_gym["run_id"] == day["run_id"]    # 取代核心条目的新条目仍在核心档案
    old_iv = memories.get(interview["id"])
    assert old_iv["superseded_by"] and old_iv["valid_until"] is None     # UPDATE 不算「情况变了」
    assert store.get("memory.watermark") == 4                            # 9/23 最后一行;9/24 的没动
    assert [c[0] for c in calls] == ["extract", "reconcile"]
    assert "〔実行済み: 70.1kg 記録したよ〕" in calls[0][1] and "おはよう" not in calls[0][1]


async def test_failure_keeps_watermark_and_alerts_second_night(fresh_db, monkeypatch, fake_embedder, sent_notices):
    _chat("2026-09-23T10:00", "今日は疲れた")
    _script(monkeypatch, None, "JSON じゃない", None, None)     # 每晚重试一次,两晚都失败
    _at(NIGHT)
    r1 = await consolidate.run()
    assert "error" in r1 and store.get("memory.watermark") == 0 and store.get("memory.fail_streak") == 1
    assert sent_notices == []
    _at(NIGHT + timedelta(days=1))
    await consolidate.run()
    assert store.get("memory.fail_streak") == 2 and len(sent_notices) == 1


async def test_embedding_down_aborts_before_llm(fresh_db, monkeypatch):
    _chat("2026-09-23T10:00", "今日は疲れた")
    calls = _script(monkeypatch)
    _at(NIGHT)
    r = await consolidate.run()
    assert "向量模型不可用" in r["error"] and calls == [] and store.get("memory.watermark") == 0


async def test_rerun_is_noop_and_diary_unique(fresh_db, monkeypatch, fake_embedder):
    _chat("2026-09-23T10:00", "ジムに行った")
    _script(monkeypatch, {"memories": [{"kind": "event", "text": "9/23 にジムに行った",
                                        "event_at": "2026-09-23 10:00"}], "diary": "ジムに行った日。"})
    _at(NIGHT)
    await consolidate.run()
    calls = _script(monkeypatch)
    r = await consolidate.run()
    assert r["days"] == [] and calls == []
    assert len(memories.list_rows(kind="diary")) == 1


async def test_backlog_processed_day_by_day(fresh_db, monkeypatch, fake_embedder):
    for d in (21, 22, 23):
        _chat(f"2026-09-{d}T12:00", f"{d}日の話")
    _chat("2026-09-24T05:00", "今日の話")
    _script(monkeypatch, *[{"memories": [], "diary": f"9/{d} の日記"} for d in (21, 22, 23)])
    _at(NIGHT)
    r = await consolidate.run()
    assert [d["day"] for d in r["days"]] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    runs = db.get_db().execute("SELECT day, hi_id FROM memory_runs ORDER BY id").fetchall()
    assert [(x["day"], x["hi_id"]) for x in runs] == [("2026-09-21", 2), ("2026-09-22", 4), ("2026-09-23", 6)]
    assert store.get("memory.watermark") == 6


async def test_too_many_rewrites_degrades_to_add(fresh_db, monkeypatch, fake_embedder):
    olds = [memories.add("fact", f"メモその{i}です", source="user") for i in range(6)]
    await memories.ensure_vectors()
    _chat("2026-09-23T10:00", "いろいろ変わった")
    monkeypatch.setattr(consolidate, "NEIGHBOR_SIM", 0.0)
    _script(monkeypatch,
            {"memories": [{"kind": "fact", "text": f"メモその{i}を更新", "event_at": None} for i in range(6)],
             "diary": None},
            {"decisions": [{"c": i, "op": "SUPERSEDE", "id": olds[i]["id"]} for i in range(6)]})
    _at(NIGHT)
    [day] = (await consolidate.run())["days"]
    assert day["guard"] is True and day["added"] == 6 and day["superseded"] == 0
    assert all(memories.get(o["id"])["active"] for o in olds)


async def test_target_forgotten_during_run_is_not_resurrected(fresh_db, monkeypatch, fake_embedder):
    """对账 await 期间用户「忘掉」了目标:不碰旧行、也不把含它内容的合并句写进去,按候选原文新增。"""
    old = memories.add("fact", "9/30 に面接がある", source="user")
    await memories.ensure_vectors()
    _chat("2026-09-23T10:00", "面接は14時から")
    monkeypatch.setattr(consolidate, "NEIGHBOR_SIM", 0.0)

    async def fake_chat(messages, tools=None, timeout=45, **kw):
        if "逐条判定" in messages[0]["content"]:
            memories.deactivate(old["id"], reason="user")
            content = {"decisions": [{"c": 0, "op": "UPDATE", "id": old["id"], "text": "9/30 14:00 に面接がある"}]}
        else:
            content = {"memories": [{"kind": "fact", "text": "面接は14時から", "event_at": None}], "diary": None}
        return {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}

    monkeypatch.setattr(llm, "chat", fake_chat)
    _at(NIGHT)
    [day] = (await consolidate.run())["days"]
    assert day["conflict"] == 1 and day["added"] == 1
    assert [m["text"] for m in memories.list_rows()] == ["面接は14時から"]
    assert memories.get(old["id"])["active"] == 0 and memories.get(old["id"])["superseded_by"] is None


async def test_undo_restores_and_rewinds_watermark(fresh_db, monkeypatch, fake_embedder):
    gym = memories.add("preference", "通っているジムはサクラフィット", source="user")
    await memories.ensure_vectors()
    _chat("2026-09-23T10:00", "ジムを変えた")
    monkeypatch.setattr(consolidate, "NEIGHBOR_SIM", 0.0)
    _script(monkeypatch,
            {"memories": [{"kind": "preference", "text": "通っているジムはゴールドジム", "event_at": None}],
             "diary": "ジムを変えた日。"},
            {"decisions": [{"c": 0, "op": "SUPERSEDE", "id": gym["id"]}]})
    _at(NIGHT)
    [day] = (await consolidate.run())["days"]
    assert consolidate.undo(day["run_id"])["reverted"] == 2          # 新条目 + 日记
    restored = memories.get(gym["id"])
    assert restored["active"] == 1 and restored["superseded_by"] is None
    assert [m["text"] for m in memories.list_rows()] == ["通っているジムはサクラフィット"]
    assert store.get("memory.watermark") == 0                          # 下次整理会重做这一天
    with pytest.raises(ValueError):
        consolidate.undo(day["run_id"])


async def test_stuck_day_degrades_then_skips(fresh_db, monkeypatch, fake_embedder, sent_notices):
    _chat("2026-09-23T10:00", "なにか")
    store.set("memory.stuck", {"day": "2026-09-23", "count": 3})
    _script(monkeypatch, None, None)                  # 降级后抽取还是失败 → 跳过这天,别让积压越堆越多
    _at(NIGHT)
    await consolidate.run()
    assert store.get("memory.watermark") == 2 and len(sent_notices) == 1
    assert db.get_db().execute("SELECT status FROM memory_runs").fetchone()["status"] == "skipped"


async def test_disabled_skips_and_dry_run_writes_nothing(fresh_db, monkeypatch, fake_embedder):
    _chat("2026-09-23T10:00", "ジムに行った")
    store.set("memory.enabled", False)
    _at(NIGHT)
    assert await consolidate.run() == {"skipped": "disabled"}
    _script(monkeypatch, {"memories": [{"kind": "event", "text": "9/23 にジムに行った",
                                        "event_at": "2026-09-23 10:00"}], "diary": "ジムの日。"})
    r = await consolidate.run(dry_run=True)
    assert r["days"][0]["items"][0]["text"] == "9/23 にジムに行った"
    assert memories.list_rows() == [] and store.get("memory.watermark") == 0


async def test_reembed_after_model_change(fresh_db, fake_embedder):
    memories.add("fact", "ピーナッツアレルギーがある", source="user")
    assert await memories.ensure_vectors() == 1
    assert await memories.ensure_vectors() == 0
    store.set("memory.embed_model", "another-model")
    assert memories.stats()["missing_vectors"] == 1
    assert await memories.ensure_vectors() == 1


async def test_about_hint_reconciles_differently_worded_change(fresh_db, monkeypatch, fake_embedder):
    """「変わった」和「通っている」措辞差太远、向量配不上:抽取时 LLM 看过相关旧记忆并标了 about,
    旧条目照样进对账被取代(bge-m3 实测这类配对只有 0.42,比无关句子还低)。"""
    gym = memories.add("preference", "通っているジムはサクラフィット", source="user")
    await memories.ensure_vectors()
    _chat("2026-09-23T10:00", "健身房换成ゴールドジム了")
    monkeypatch.setattr(consolidate, "RELATED_SIM", 0.0)          # 假向量下也让它出现在「相关的已有记忆」里
    cand = "ゴールドジムに変わった"
    assert float(fake_embedder(cand) @ fake_embedder(gym["text"])) < consolidate.NEIGHBOR_SIM
    calls = _script(monkeypatch,
                    {"memories": [{"kind": "preference", "text": cand, "event_at": None,
                                   "about": [gym["id"], 9999]}], "diary": None},
                    {"decisions": [{"c": 0, "op": "SUPERSEDE", "id": gym["id"]}]})
    _at(NIGHT)
    [day] = (await consolidate.run())["days"]
    assert f"#{gym['id']}" in calls[0][1]                          # 抽取时看到了旧记忆和它的 id
    assert [c[0] for c in calls] == ["extract", "reconcile"]
    assert day["superseded"] == 1 and memories.get(gym["id"])["active"] == 0

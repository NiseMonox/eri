"""长期记忆库 v6:迁移、每轮消息结构(核心档案/当前状态/相关记忆)与前缀稳定、actions 落库、
remember / forget / search_memory。向量用 conftest 的 fake_embedder,LLM 用 test_conversation 的脚本回放。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_conversation import _fake_llm, _tool_results

from app import clock, db, store
from app.services import conversation, memories

T0 = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)   # 9/23(水)JST 12:00


def test_migration_v5_to_v6_starts_from_now(tmp_path):
    """v5 库升级:水位线 = 当前最大 chat_log.id(从现在开始整理),旧记忆原样不动,升级前自动备份。"""
    p = tmp_path / "v5.db"
    conn = sqlite3.connect(p)
    conn.executescript((Path(db.__file__).parent / "schema.sql").read_text())
    for v in (2, 3, 4, 5):
        for stmt in db.MIGRATIONS[v].split(";"):
            if stmt.strip():
                conn.execute(stmt)
    conn.execute("INSERT INTO chat_log (ts, via, role, text) VALUES ('2026-09-09T03:00:00Z','siri','user','旧対話')")
    conn.execute("INSERT INTO chat_log (ts, via, role, text) VALUES ('2026-09-09T03:00:01Z','siri','assistant','ok')")
    conn.execute("INSERT INTO memories (kind, text, source, active, created_at, updated_at) "
                 "VALUES ('fact','古いテスト','assistant',0,'2026-08-16T06:23:10Z','2026-08-16T06:23:17Z')")
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    conn.close()

    c = db.init_db(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
    assert store.get("memory.watermark") == 2
    old = dict(c.execute("SELECT * FROM memories").fetchone())
    assert old["active"] == 0 and old["core"] == 0 and old["superseded_by"] is None
    assert (tmp_path / "backups" / "pre-v6-migration.db").exists()


async def test_turn_layout_and_stable_prefix(fresh_db, monkeypatch, fake_embedder):
    """system = 人设 + 核心档案;最后一条 = 当前状态 + 原话;隔几分钟的几轮之间,历史前缀逐字节不变。"""
    clock.set_override(T0)
    memories.add("preference", "朝は催促されたくない", core=True, source="user")
    turns = []
    for i, text in enumerate(("最近どう?", "そうなんだ", "ありがとう")):
        clock.set_override(T0 + timedelta(minutes=5 * i))
        seen = _fake_llm(monkeypatch, "うん")
        await conversation.handle(text, via="telegram")
        turns.append(seen[0])
    first, second, third = turns
    assert "【核心档案】" in first[0]["content"] and "朝は催促されたくない" in first[0]["content"]
    assert first[-1]["content"].startswith("【当前状态】") and first[-1]["content"].endswith("最近どう?")
    assert third[:len(second) - 1] == second[:-1]           # 人设 + 核心档案 + 历史:只往后追加
    assert third[len(second) - 1]["content"] == "[09/23 12:05] そうなんだ"


async def test_related_memories_threshold_and_exclusions(fresh_db, fake_embedder):
    clock.set_override(T0)
    store.set("memory.rag_min_sim", 0.5)
    q = "通っているジムはどこ"
    hit = memories.add("fact", "通っているジムはサクラフィット", source="user")
    core = memories.add("preference", "通っているジムは朝が空いている", core=True, source="user")
    far = memories.add("fact", "ピーナッツアレルギーがある", source="user")
    await memories.ensure_vectors()
    assert float(fake_embedder(q) @ fake_embedder(hit["text"])) > 0.5      # 前提:假向量下确实相近
    rows, top5 = memories.related(fake_embedder(q))
    ids = [r["id"] for r in rows]
    assert hit["id"] in ids and core["id"] not in ids and far["id"] not in ids     # 核心档案已常驻,不重复带
    assert top5 and top5[0][0] == hit["id"]
    # 来源对话还在原文窗口里的,不重复带
    db.get_db().execute("UPDATE memories SET src_hi=10 WHERE id=?", (hit["id"],))
    db.get_db().commit()
    memories.invalidate()
    rows, _ = memories.related(fake_embedder(q), hist_first_id=5)
    assert hit["id"] not in [r["id"] for r in rows]


async def test_no_embedding_service_still_replies(fresh_db, monkeypatch):
    """Ollama 不在(conftest 默认):不带相关记忆,照常回复。"""
    clock.set_override(T0)
    memories.add("fact", "ピーナッツアレルギーがある", source="user")
    seen = _fake_llm(monkeypatch, "OK")
    res = await conversation.handle("何か食べたい", via="telegram")
    assert res["reply"] == "OK" and "【相关记忆】" not in seen[0][-1]["content"]


async def test_actions_logged_and_shown_in_history(fresh_db, monkeypatch):
    clock.set_override(T0)
    _fake_llm(monkeypatch, [("record_weight", {"weight_kg": 70.2, "measured_at": None}), ("show_today", {})],
              "記録したよ〔実行済み: まねっこ〕")
    res = await conversation.handle("70.2、あと今日の予定", via="telegram")
    assert res["reply"] == "記録したよ"                                   # 模型学着写的标注被删掉
    acts = json.loads(db.get_db().execute("SELECT actions FROM chat_log WHERE role='assistant'").fetchone()[0])
    assert [a["tool"] for a in acts] == ["record_weight", "show_today"]
    assert "70.2kg" in acts[0]["result"] and acts[1]["result"] is None     # 只读工具只记名字

    seen = _fake_llm(monkeypatch, "どういたしまして")
    await conversation.handle("ありがとう", via="telegram")
    past = [m for m in seen[0] if m["role"] == "assistant"][0]["content"]
    assert past.startswith("〔実行済み: 70.2kg 記録したよ") and past.endswith("記録したよ")


async def test_remember_dedupes_rejects_relative_dates_and_caps_core(fresh_db, monkeypatch, fake_embedder):
    clock.set_override(T0)
    store.set("memory.core_max", 1)
    seen = _fake_llm(monkeypatch,
                     [("remember", {"text": "ピーナッツアレルギーがある", "kind": "fact", "core": True}),
                      ("remember", {"text": "明日は傘を持っていく", "kind": "schedule"}),
                      ("remember", {"text": "コーヒーはブラック派", "kind": "preference", "core": True})],
                     [("remember", {"text": "ピーナッツアレルギーがある", "kind": "fact"})],
                     "覚えたよ")
    await conversation.handle("记住我花生过敏、明天带伞、咖啡只喝黑的", via="telegram")
    allergy, umbrella, coffee, again = _tool_results(seen[2])
    assert allergy["ok"] and umbrella["ok"] is False and "日付" in umbrella["result"]
    assert coffee["ok"] and "満杯" in coffee["result"]                     # 核心档案满了 → 普通条目
    assert again["ok"] and "もう覚えてる" in again["result"]               # 同一句不重复记
    assert [m["text"] for m in memories.core_rows()] == ["ピーナッツアレルギーがある"]
    assert len(memories.list_rows()) == 2


async def test_forget_only_ids_visible_this_turn(fresh_db, monkeypatch, fake_embedder):
    """forget 只能动本轮 LLM 看得到的记忆(核心档案/近日の予定/相关记忆/检索结果),幻觉 id 动不了库里别的。"""
    clock.set_override(T0)
    shown = memories.add("preference", "朝は催促されたくない", core=True, source="user")
    hidden = memories.add("fact", "ピーナッツアレルギーがある", source="user")
    seen = _fake_llm(monkeypatch, [("forget_memory", {"memory_id": hidden["id"]}),
                                   ("forget_memory", {"memory_id": shown["id"]})], "忘れたよ")
    await conversation.handle("早上催我也没关系了", via="telegram")
    bad, ok = _tool_results(seen[1])
    assert bad["ok"] is False and ok["ok"]
    assert memories.get(hidden["id"])["active"] == 1 and memories.get(shown["id"])["active"] == 0


async def test_search_memory_by_date_includes_raw_words(fresh_db, monkeypatch):
    """「上周三我干嘛了」:按日期返回那天的记忆和用户原话(整理没记下的细节也找得到)。"""
    clock.set_override(T0)
    conversation._log("telegram", "user", "今日は面接の練習をした", ts="2026-09-16T05:00:00Z")   # 9/16(水)14:00
    memories.add("event", "9/16 に面接の練習をした", event_at="2026-09-16T05:00:00Z", source="user")
    seen = _fake_llm(monkeypatch, [("search_memory", {"query": None, "date_from": "2026-09-16", "date_to": None})],
                     "練習してたね")
    await conversation.handle("上周三我干嘛了", via="telegram")
    result = _tool_results(seen[1])[0]["result"]
    assert "9/16 に面接の練習をした" in result and "ユーザーの発言「今日は面接の練習をした」" in result

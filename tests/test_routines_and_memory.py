"""v5:routine 统一、长期记忆、3 天上下文预算。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import clock, db, store
from app.services import conversation, memories, reminders, routines

T0 = datetime(2026, 8, 16, 1, 0, tzinfo=timezone.utc)   # JST 10:00


def _advance(minutes):
    clock.set_override(T0 + timedelta(minutes=minutes))


def test_migration_v4_to_v5_maps_meds(tmp_path):
    """带 meds/med_logs/med schedule 的 v4 库升到 v5:数据映射正确,旧表保留。"""
    p = tmp_path / "v4.db"
    conn = sqlite3.connect(p)
    conn.executescript((Path(db.__file__).parent / "schema.sql").read_text())
    for v in (2, 3, 4):
        for stmt in db.MIGRATIONS[v].split(";"):
            if stmt.strip():
                conn.execute(stmt)
    conn.execute("INSERT INTO meds (id,name,dose,notes,active,created_at) VALUES (7,'降压药','10mg','饭后',1,'2026-08-01T00:00:00Z')")
    conn.execute("INSERT INTO med_logs (med_id,schedule_id,due_at,status,confirmed_at,confirm_via,remind_count,token,params,created_at) "
                 "VALUES (7,3,'2026-08-10T00:00:00Z','confirmed','2026-08-10T00:05:00Z','bark',1,'tok1','{\"resend_every_min\":30,\"max_resends\":3,\"grace_min\":180}','2026-08-10T00:00:00Z')")
    conn.execute("INSERT INTO med_logs (med_id,due_at,status,remind_count,token,created_at) VALUES (7,'2026-08-11T00:00:00Z','skipped',0,'tok2','2026-08-11T00:00:00Z')")
    conn.execute("INSERT INTO schedules (id,name,type,cron,payload,enabled,created_at,updated_at) VALUES (3,'早药','med','0 8 * * *','{\"med_id\":7}',1,'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')")
    conn.execute("PRAGMA user_version=4")
    conn.commit(); conn.close()

    c = db.init_db(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 5
    rt = dict(c.execute("SELECT * FROM routines WHERE id=7").fetchone())
    assert rt["name"] == "降压药" and rt["category"] == "med" and "10mg" in rt["detail"]
    inst = [dict(r) for r in c.execute("SELECT * FROM reminders WHERE kind='routine' ORDER BY due_at")]
    assert len(inst) == 2
    assert inst[0]["status"] == "done" and inst[0]["done_via"] == "bark" and inst[0]["token"] == "tok1"
    assert json.loads(inst[0]["nag"])["every_min"] == 30      # 参数名已对齐
    assert inst[1]["status"] == "dismissed"
    sch = dict(c.execute("SELECT * FROM schedules WHERE id=3").fetchone())
    assert sch["type"] == "routine" and json.loads(sch["payload"])["routine_id"] == 7
    assert c.execute("SELECT COUNT(*) FROM _legacy_meds").fetchone()[0] == 1   # 旧表保留
    c.execute("SELECT COUNT(*) FROM memories")


def test_routine_instance_flow(fresh_db):
    clock.set_override(T0)
    rt = routines.create("ストレッチ", "care", nag={"every_min": 30, "max": 2, "grace_min": 90})
    inst = routines.create_instance(rt, schedule_id=5)
    assert inst["kind"] == "routine" and inst["token"]
    assert routines.create_instance(rt, schedule_id=5) is None      # 未关闭 → 去重
    reminders.mark_notified(inst["id"])
    _advance(31)
    acts = reminders.sweep_nag()
    assert acts and acts[0][1] == "renag" and acts[0][0]["kind"] == "routine"
    # 「やった」→ done,带渠道
    row = routines.complete(inst["id"], via="siri")
    assert row["status"] == "done" and row["done_via"] == "siri"
    assert routines.completion_rate(rt["id"], 7)["done"] == 1
    # 关闭后可再实例化(晚间第二次)
    assert routines.create_instance(rt, schedule_id=5) is not None
    hm = routines.heatmap(2)
    assert "2026-08-16" in hm[rt["id"]]
    assert routines.latest_open("med") is None
    assert routines.latest_open() is not None


def test_memory_crud_expiry_and_guard(fresh_db):
    clock.set_override(T0)
    m1 = memories.add("preference", "毎週水曜はジムに行く")
    m2 = memories.add("schedule", "8/20 14:00 面接", valid_until=clock.iso(T0 + timedelta(days=4)))
    m3 = memories.add("mood", "最近ちょっと疲れ気味")
    assert m3["valid_until"] is not None                          # mood 默认 7 天
    assert len(memories.list_active()) == 3
    assert "毎週水曜" in memories.context_block()

    # apply_ops:幻觉 id 忽略;schedule 无日期拒收;update/remove 生效
    n = memories.apply_ops({
        "add": [{"kind": "schedule", "text": "无日期的安排"},               # 拒收
                {"kind": "fact", "text": "2028卒で就活中"}],
        "update": [{"id": m1["id"], "text": "毎週火曜と木曜はジムに行く"}, {"id": 9999, "text": "x"}],
        "remove": [m2["id"], 8888],
    })
    assert n == {"add": 1, "update": 1, "remove": 1}
    assert memories.get(m1["id"])["text"].startswith("毎週火曜")
    assert memories.get(m2["id"])["active"] == 0

    # 过期自动失效
    _advance(8 * 24 * 60)
    assert all(m["kind"] != "mood" for m in memories.list_active())


async def test_nightly_cleanup_expires(fresh_db, monkeypatch):
    clock.set_override(T0)
    memories.add("schedule", "昨日の予定", valid_until=clock.iso(T0 - timedelta(hours=1)))
    memories.add("fact", "長期の事実")

    async def no_llm(*a, **k):
        return None

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", no_llm)
    n = await memories.nightly_cleanup()
    assert n["expired"] == 1
    assert [m["text"] for m in memories.list_active()] == ["長期の事実"]


async def test_conversation_forget_and_memory_maintenance(fresh_db, monkeypatch):
    clock.set_override(T0)
    m = memories.add("preference", "朝は催促されたくない")
    calls = []

    async def fake_llm(prompt, system="", timeout=45, purpose="parse"):
        calls.append(system[:20])
        if "记忆管理器" in system:
            return json.dumps({"add": [{"kind": "fact", "text": "健身房はサクラフィット"}], "update": [], "remove": []})
        assert "朝は催促されたくない" in prompt          # 记忆注入进上下文
        return json.dumps({"action": "forget", "memory_id": m["id"]})

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", fake_llm)
    res = await conversation.handle("那个别记了", via="siri")
    assert res["ok"] and "忘れた" in res["reply"]
    assert memories.get(m["id"])["active"] == 0
    # 异步维护任务跑完
    import asyncio

    await asyncio.sleep(0.05)
    assert any(x["text"] == "健身房はサクラフィット" for x in memories.list_active())


def test_recent_chat_budget(fresh_db):
    clock.set_override(T0)
    store.set("chat.context_budget_tokens", 60)
    for i in range(20):
        conversation._log("siri", "user", "あ" * 20)          # 每条 ≈21 token
    picked = conversation._recent_chat()
    assert 0 < len(picked) <= 3                                # 预算内截断
    # 3 天前的不进
    conn = db.get_db()
    conn.execute("UPDATE chat_log SET ts=?", (clock.iso(T0 - timedelta(days=4)),))
    conn.commit()
    assert conversation._recent_chat() == []

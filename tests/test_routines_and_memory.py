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
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
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


def test_recent_chat_budget(fresh_db):
    clock.set_override(T0)
    store.set("chat.context_budget_tokens", 60)
    for i in range(20):
        conversation._log("siri", "user", "あ" * 20)          # 每条 ≈21 token
    picked = conversation._recent_chat()
    assert 0 < len(picked) <= 3                                # 预算内截断
    # 窗口 = 昨天 04:00 起 ∪ 还没整理进记忆的:4 天前但没整理的仍在(整理失败也不丢),整理过的才出窗口
    conn = db.get_db()
    conn.execute("UPDATE chat_log SET ts=?", (clock.iso(T0 - timedelta(days=4)),))
    conn.commit()
    assert len(conversation._recent_chat()) > 0
    store.set("memory.watermark", conn.execute("SELECT MAX(id) FROM chat_log").fetchone()[0])
    assert conversation._recent_chat() == []


def test_routine_two_schedules_independent(fresh_db):
    """同 routine 的早晚两个 schedule 各自实例化(去重按 schedule_id)。"""
    clock.set_override(T0)
    rt = routines.create("降压药", "med")
    a = routines.create_instance(rt, schedule_id=1)
    reminders.mark_notified(a["id"])
    reminders.snooze(a["id"], clock.iso(T0 + timedelta(hours=10)))   # 早上的推迟到晚上
    b = routines.create_instance(rt, schedule_id=2)                    # 晚间 schedule 照常
    assert b is not None and b["id"] != a["id"]
    assert routines.create_instance(rt, schedule_id=2) is None         # 同 schedule 才去重


def test_routine_skip_accepts_missed(fresh_db):
    clock.set_override(T0)
    rt = routines.create("ジム", "exercise", nag={"every_min": 1, "max": 1, "grace_min": 2})
    inst = routines.create_instance(rt, schedule_id=3)
    reminders.mark_notified(inst["id"])
    _advance(3)
    reminders.sweep_nag()
    assert reminders.get(inst["id"])["status"] == "missed"
    row = routines.skip(inst["id"], via="siri")
    assert row["status"] == "dismissed"

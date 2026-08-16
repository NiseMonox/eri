"""对话式秘书:迁移、snooze/追催状态机、conversation 动作执行(mock LLM)。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import clock, db
from app.services import conversation, reminders

T0 = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)   # JST 10:00


def _advance(minutes):
    clock.set_override(T0 + timedelta(minutes=minutes))


def _make_v1_db(p):
    conn = sqlite3.connect(p)
    conn.executescript((Path(db.__file__).parent / "schema.sql").read_text())
    conn.execute("INSERT INTO reminders (title, body, due_at, created_at) VALUES ('旧数据','', '2026-08-15T01:00:00Z','2026-08-15T00:00:00Z')")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()


def test_migration_v1_to_latest(tmp_path):
    """现有 v1 生产库升级后新列/新表齐备且旧数据保留。"""
    p = tmp_path / "old.db"
    _make_v1_db(p)
    c2 = db.init_db(p)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = dict(c2.execute("SELECT * FROM reminders").fetchone())
    assert row["title"] == "旧数据" and row["remind_count"] == 0 and row["notified_at"] is None
    c2.execute("SELECT COUNT(*) FROM chat_log")   # 表存在


def test_migration_atomic_on_failure(tmp_path):
    """迁移中途失败必须整体回滚:版本号不动、列没加,修好后可重试(不 boot loop)。"""
    import pytest

    p = tmp_path / "old.db"
    _make_v1_db(p)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE chat_log (x INTEGER)")   # 人为制造 v2 的 CREATE TABLE 冲突
    conn.commit()
    conn.close()

    with pytest.raises(Exception):
        db.init_db(p)
    check = sqlite3.connect(p)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1          # 版本没动
    cols = [r[1] for r in check.execute("PRAGMA table_info(reminders)")]
    assert "schedule_id" not in cols                                        # ALTER 已回滚
    check.execute("DROP TABLE chat_log")
    check.commit()
    check.close()
    c2 = db.init_db(p)                                                      # 修好后可重试
    assert c2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_instance_dedupe_and_nag_flow(fresh_db):
    clock.set_override(T0)
    inst = reminders.create_instance("去健身房", schedule_id=42,
                                     nag={"every_min": 30, "max": 3, "grace_min": 120})
    assert inst is not None
    # 同 schedule 还有未关闭实例 → 不再建(防 misfire 重复)
    assert reminders.create_instance("去健身房", schedule_id=42) is None
    # 但前一实例关闭后(如晚间第二时段),照常实例化
    reminders.set_status(inst["id"], "done")
    inst2 = reminders.create_instance("去健身房", schedule_id=42,
                                      nag={"every_min": 30, "max": 3, "grace_min": 120})
    assert inst2 is not None
    reminders.set_status(inst2["id"], "dismissed")
    inst = reminders.create_instance("去健身房", schedule_id=42,
                                     nag={"every_min": 30, "max": 3, "grace_min": 120})

    reminders.mark_notified(inst["id"])
    _advance(10)
    assert reminders.sweep_nag() == []
    _advance(31)
    acts = reminders.sweep_nag()
    assert acts[0][1] == "renag" and acts[0][0]["remind_count"] == 1
    assert reminders.sweep_nag() == []          # 同刻不重复
    _advance(61)
    assert reminders.sweep_nag()[0][1] == "renag"
    _advance(121)
    acts = reminders.sweep_nag()
    assert acts and acts[0][1] == "missed"
    assert reminders.get(inst["id"])["status"] == "missed"


def test_snooze_resets_and_refires(fresh_db):
    clock.set_override(T0)
    inst = reminders.create_instance("去健身房", schedule_id=1)
    reminders.mark_notified(inst["id"])
    _advance(31)
    assert reminders.sweep_nag()[0][1] == "renag"

    # 「下午再去」→ snooze 到 14:00 JST
    row = reminders.snooze(inst["id"], "2026-08-15T05:00:00Z")
    assert row["status"] == "pending" and row["remind_count"] == 0
    # 14:00 前 sweeper 不理它
    _advance(120)   # JST 12:00
    assert reminders.due_pending() == []
    assert reminders.sweep_nag() == []
    # 14:01 到点重新进入首播队列
    _advance(241)
    due = reminders.due_pending()
    assert len(due) == 1 and due[0]["id"] == inst["id"]


async def test_conversation_snooze_and_done(fresh_db, monkeypatch):
    clock.set_override(T0)
    inst = reminders.create_instance("去健身房", schedule_id=7)
    reminders.mark_notified(inst["id"])

    async def no_tts(*a, **k):
        return False

    from app.audio import tts

    monkeypatch.setattr(tts, "announce_snoozed", no_tts)
    monkeypatch.setattr(tts, "announce_done", no_tts)

    async def fake_llm_snooze(prompt, system="", timeout=45):
        assert "去健身房" in prompt      # 上下文里能看到开放事项
        return json.dumps({"action": "snooze", "reminder_id": inst["id"],
                           "until": "2026-08-15 14:00"})

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", fake_llm_snooze)
    res = await conversation.handle("手头有点事,下午再去", via="siri")
    assert res["ok"] and "14:00" in res["reply"]
    assert reminders.get(inst["id"])["status"] == "pending"

    async def fake_llm_done(prompt, system="", timeout=45):
        assert "手头有点事" in prompt    # chat_log 进入了上下文
        return json.dumps({"action": "done", "reminder_id": inst["id"]})

    monkeypatch.setattr(llm, "complete", fake_llm_done)
    res2 = await conversation.handle("那我去健身咯", via="siri")
    assert res2["ok"] and "完了" in res2["reply"]
    assert reminders.get(inst["id"])["status"] == "done"
    assert len(reminders.done_today()) == 1


async def test_conversation_fast_path_no_llm(fresh_db, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("正则快路径不该碰 LLM")

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", boom)
    res = await conversation.handle("62.5", via="telegram")
    assert res["ok"] and "62.5" in res["reply"]


async def test_conversation_rejects_hallucinated_id(fresh_db, monkeypatch):
    """LLM 幻觉出的 id(不在开放事项里)不能动历史行/未来行。"""
    clock.set_override(T0)
    inst = reminders.create_instance("已完成的旧事", schedule_id=1)
    reminders.mark_notified(inst["id"])
    reminders.set_status(inst["id"], "done")          # 历史行,不在 open_items

    async def fake_llm(prompt, system="", timeout=45):
        return json.dumps({"action": "snooze", "reminder_id": inst["id"],
                           "until": "2026-08-15 14:00"})

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", fake_llm)
    res = await conversation.handle("晚点再说", via="siri")
    assert res["ok"] is False and "見つからなかった" in res["reply"]
    assert reminders.get(inst["id"])["status"] == "done"   # 没被复活


async def test_missed_can_be_completed_late(fresh_db, monkeypatch):
    """作罢(missed)的事项仍在开放事项里,「补做了」能兑现。"""
    clock.set_override(T0)
    inst = reminders.create_instance("倒垃圾", schedule_id=2,
                                     nag={"every_min": 1, "max": 1, "grace_min": 2})
    reminders.mark_notified(inst["id"])
    _advance(3)
    acts = reminders.sweep_nag()
    assert acts and acts[-1][1] == "missed"
    assert any(r["id"] == inst["id"] for r in reminders.open_items())

    async def no_tts(*a, **k):
        return False

    from app.audio import tts

    monkeypatch.setattr(tts, "announce_done", no_tts)

    async def fake_llm(prompt, system="", timeout=45):
        return json.dumps({"action": "done", "reminder_id": inst["id"]})

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", fake_llm)
    res = await conversation.handle("垃圾我刚才倒了", via="telegram")
    assert res["ok"] and reminders.get(inst["id"])["status"] == "done"


async def test_conversation_llm_garbage(fresh_db, monkeypatch):
    async def bad_llm(*a, **k):
        return "我觉得你应该去健身(这不是JSON)"

    from app.llm import base as llm

    monkeypatch.setattr(llm, "complete", bad_llm)
    res = await conversation.handle("嗯呢那个啥", via="siri")
    assert res["reply"]     # 不炸,拿到兜底回复

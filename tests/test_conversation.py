"""对话式秘书:迁移、snooze/追催状态机、conversation 工具调用循环(mock LLM)。"""

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import clock, db, events
from app.services import conversation, intents, reminders, routines, weights

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


def _fake_llm(monkeypatch, *steps):
    """按顺序回放的假 LLM。每步:[(工具名, 参数), ...] = 发起工具调用;str = 最终回复;
    dict = 原样返回的 assistant message;None = 调用失败。返回每次收到的 messages 快照。"""
    seen = []
    it = iter(steps)

    async def fake_chat(messages, tools=None, timeout=45):
        seen.append(copy.deepcopy(messages))
        step = next(it)
        if step is None or isinstance(step, dict):
            return step
        if isinstance(step, str):
            return {"role": "assistant", "content": step}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            for i, (name, args) in enumerate(step)]}

    async def no_complete(*a, **k):
        return None

    from app.llm import base as llm

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "complete", no_complete)   # 后台记忆维护别吃掉脚本步骤
    return seen


def _tool_results(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


@pytest.fixture()
def no_tts(monkeypatch):
    from app.audio import tts

    async def quiet(*a, **k):
        return False

    monkeypatch.setattr(tts, "announce_snoozed", quiet)
    monkeypatch.setattr(tts, "announce_done", quiet)


async def test_conversation_snooze_and_done(fresh_db, monkeypatch, no_tts):
    clock.set_override(T0)
    inst = reminders.create_instance("去健身房", schedule_id=7)
    reminders.mark_notified(inst["id"])

    seen = _fake_llm(monkeypatch,
                     [("snooze", {"reminder_id": inst["id"], "until": "2026-08-15 14:00"})],
                     "OK、14時にまた声かけるね👍")
    res = await conversation.handle("手头有点事,下午再去", via="siri")
    assert res["ok"] and res["reply"] == "OK、14時にまた声かけるね"     # LLM 写的回复;Siri 去掉 emoji
    assert "去健身房" in seen[0][-1]["content"]                             # 开放事项在本轮消息的【当前状态】里
    assert "14:00" in _tool_results(seen[1])[0]["result"]                 # 执行结果回喂给 LLM
    assert reminders.get(inst["id"])["status"] == "pending"

    seen = _fake_llm(monkeypatch, [("mark_done", {"reminder_id": inst["id"]})], "えらい!")
    res2 = await conversation.handle("那我去健身咯", via="siri")
    assert any(m["role"] == "user" and "手头有点事" in m["content"] for m in seen[0])  # 上一轮进了多轮历史
    assert res2["ok"] and reminders.get(inst["id"])["status"] == "done"
    assert len(reminders.done_today()) == 1


async def test_multiple_intents_in_one_message(fresh_db, monkeypatch):
    clock.set_override(T0)
    seen = _fake_llm(monkeypatch,
                     [("record_weight", {"weight_kg": 62.3, "measured_at": None}),
                      ("create_reminder", {"title": "取快递", "due_at": "2026-08-16 15:00"})],
                     "62.3kg記録して、明日15時のリマインダーもセットしたよ")
    res = await conversation.handle("今天早上称了62.3,明天下午三点提醒我去取快递", via="telegram")
    assert res["ok"] and "62.3kg" in res["reply"]
    assert [r["ok"] for r in _tool_results(seen[1])] == [True, True]
    assert weights.stats(7)["last"] == 62.3
    assert [r["title"] for r in reminders.upcoming()] == ["取快递"]


async def test_upcoming_reminder_can_be_rescheduled(fresh_db, monkeypatch, no_tts):
    """明天以后的提醒也在上下文里,「改到 4 点」有对象可指;改到过去的时刻被拒,LLM 可以改正。"""
    clock.set_override(T0)
    r = reminders.create("取快递", "", "2026-08-16T06:00:00Z")          # 明天 15:00 JST
    seen = _fake_llm(monkeypatch,
                     [("snooze", {"reminder_id": r["id"], "until": "2026-08-15 09:00"})],   # 已过
                     [("snooze", {"reminder_id": r["id"], "until": "2026-08-16 16:00"})],
                     "明日16時に変えたよ")
    await conversation.handle("明天取快递改到4点", via="telegram")
    assert "【今后的提醒】" in seen[0][-1]["content"] and "取快递" in seen[0][-1]["content"]
    first, second = _tool_results(seen[2])
    assert first["ok"] is False and "過ぎてる" in first["result"]
    assert second["ok"] and "08/16 16:00" in second["result"]
    assert reminders.get(r["id"])["due_at"] == "2026-08-16T07:00:00Z"


async def test_create_reminder_rejects_past_time(fresh_db, monkeypatch):
    clock.set_override(T0)                                              # JST 10:00
    seen = _fake_llm(monkeypatch,
                     [("create_reminder", {"title": "倒垃圾", "due_at": "2026-08-15 08:00"})],
                     "8時はもう過ぎてるよ。明日の朝にする?")
    res = await conversation.handle("8点提醒我倒垃圾", via="telegram")
    assert res["ok"] is False and _tool_results(seen[1])[0]["ok"] is False
    assert reminders.upcoming() == [] and reminders.open_items() == []


async def test_regex_fallback_when_llm_down(fresh_db, monkeypatch):
    """LLM 关闭/不可用:正则认得出的照样记录(模板回复),认不出的老实说。"""
    _fake_llm(monkeypatch, None, None)
    res = await conversation.handle("62.5", via="telegram")
    assert res["ok"] and "62.5" in res["reply"]
    res2 = await conversation.handle("今天好累", via="telegram")
    assert res2["ok"] is False and "頭がうまく回らない" in res2["reply"]


async def test_llm_fails_after_tools_uses_tool_replies(fresh_db, monkeypatch):
    """工具已执行、写回复时 LLM 挂了:不能再走正则重复记录,用工具的模板回复收尾。"""
    clock.set_override(T0)
    _fake_llm(monkeypatch, [("record_weight", {"weight_kg": 61.8, "measured_at": None})], None)
    res = await conversation.handle("61.8", via="telegram")
    assert res["ok"] and "61.8kg 記録したよ" in res["reply"]
    assert weights.stats(7)["count"] == 1


async def test_conversation_rejects_hallucinated_id(fresh_db, monkeypatch):
    """LLM 幻觉出的 id(不在开放事项/今后的提醒里)不能动历史行。"""
    clock.set_override(T0)
    inst = reminders.create_instance("已完成的旧事", schedule_id=1)
    reminders.mark_notified(inst["id"])
    reminders.set_status(inst["id"], "done")          # 历史行
    seen = _fake_llm(monkeypatch,
                     [("snooze", {"reminder_id": inst["id"], "until": "2026-08-15 14:00"})],
                     "どの件のこと?")
    res = await conversation.handle("晚点再说", via="siri")
    assert res["ok"] is False and "見つからなかった" in _tool_results(seen[1])[0]["result"]
    assert reminders.get(inst["id"])["status"] == "done"   # 没被复活


async def test_missed_can_be_completed_late(fresh_db, monkeypatch, no_tts):
    """作罢(missed)的事项仍在开放事项里,「补做了」能兑现。"""
    clock.set_override(T0)
    inst = reminders.create_instance("倒垃圾", schedule_id=2,
                                     nag={"every_min": 1, "max": 1, "grace_min": 2})
    reminders.mark_notified(inst["id"])
    _advance(3)
    acts = reminders.sweep_nag()
    assert acts and acts[-1][1] == "missed"
    assert any(r["id"] == inst["id"] for r in reminders.open_items())

    _fake_llm(monkeypatch, [("mark_done", {"reminder_id": inst["id"]})], "えらい!")
    res = await conversation.handle("垃圾我刚才倒了", via="telegram")
    assert res["ok"] and reminders.get(inst["id"])["status"] == "done"


async def test_bad_tool_calls_do_not_crash(fresh_db, monkeypatch):
    """未知工具、参数不是 JSON、执行时抛异常:错误都回喂给 LLM,不炸。"""
    async def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(intents, "execute", boom)
    seen = _fake_llm(monkeypatch,
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"id": "a", "type": "function",
                          "function": {"name": "launch_rocket", "arguments": "{}"}},
                         {"id": "b", "type": "function",
                          "function": {"name": "record_weight", "arguments": "{oops"}},
                         {"id": "c", "type": "function",
                          "function": {"name": "show_today", "arguments": ""}}]},
                     "ごめん、うまくいかなかった")
    res = await conversation.handle("嗯呢那个啥", via="siri")
    assert res["reply"] == "ごめん、うまくいかなかった" and res["ok"] is False
    assert [r["ok"] for r in _tool_results(seen[1])] == [False, False, False]
    assert events.recent(kind_prefix="tool_error")


async def test_set_recurring_via_conversation(fresh_db, monkeypatch, no_tts):
    """「拉伸隔一天晚上9点提醒我」→ 用已有 routine 设以完成为准的隔天提醒;
    提醒前就做了 → record_routine_done,下次顺延;不用了 → stop_recurring。"""
    clock.set_override(T0)                                                   # 08/15(土)10:00
    rt = routines.create("ストレッチ", "care", "20分")
    seen = _fake_llm(monkeypatch,
                     [("set_recurring", {"routine_id": rt["id"], "times": ["21:00"], "every_days": 2})],
                     "OK、1日おきに21時ね")
    await conversation.handle("拉伸隔一天晚上9点提醒我", via="telegram")
    assert f"routine_id={rt['id']}" in seen[0][-1]["content"]                 # 【ルーティン】在本轮消息里
    result = _tool_results(seen[1])[0]
    assert result["ok"] and "2日ごと 21:00" in result["result"] and "08/15(土) 21:00" in result["result"]
    [s] = routines.schedules_of(rt["id"])
    assert s["cron"] == "0 21 * * *" and s["payload"]["every_days"] == 2
    assert len(routines.list_all()) == 1                                     # 没有重复新建

    seen = _fake_llm(monkeypatch, [("record_routine_done", {"routine_id": rt["id"], "done_at": None})],
                     "えらい!")
    await conversation.handle("拉伸刚做完了", via="telegram")
    assert "08/17(月) 21:00" in _tool_results(seen[1])[0]["result"]

    _fake_llm(monkeypatch, [("stop_recurring", {"routine_id": rt["id"]})], "止めたよ")
    await conversation.handle("拉伸不用再提醒了", via="telegram")
    assert routines.schedules_of(rt["id"]) == []


async def test_set_recurring_new_routine_validates_first(fresh_db, monkeypatch):
    """新建时参数不对(隔天 + 指定星期)要先拒,不能留下一条没有提醒的空 routine。"""
    clock.set_override(T0)
    seen = _fake_llm(monkeypatch,
                     [("set_recurring", {"name": "ビタミン", "times": ["22:00"], "every_days": 2,
                                         "weekdays": [1]})],
                     [("set_recurring", {"name": "ビタミン", "times": ["22:00"], "category": "habit"})],
                     "毎日22時ね")
    await conversation.handle("每天晚上十点提醒我吃维生素", via="telegram")
    first, second = _tool_results(seen[2])
    assert first["ok"] is False and second["ok"] and "毎日 22:00" in second["result"]
    [rt] = routines.list_all()
    assert rt["name"] == "ビタミン" and rt["category"] == "habit"


async def test_mark_done_with_past_time(fresh_db, monkeypatch, no_tts):
    """「昨晚其实做了」:完成时刻记成昨晚,隔天的间隔从昨晚那天算。"""
    rt = routines.create("ストレッチ", "care")
    routines.set_schedule(rt["id"], ["21:00"], every_days=2)
    clock.set_override(T0 - timedelta(hours=13))                            # 08/14 21:00 提醒
    inst = routines.create_instance(rt, schedule_id=None)
    reminders.mark_notified(inst["id"])
    clock.set_override(T0 - timedelta(hours=10, minutes=30))                # 23:30 没理 → 作罢
    reminders.sweep_nag()
    assert reminders.get(inst["id"])["status"] == "missed"

    clock.set_override(T0)                                                   # 第二天早上才说
    _fake_llm(monkeypatch, [("mark_done", {"reminder_id": inst["id"], "done_at": "2026-08-14 22:30"})],
              "了解!")
    await conversation.handle("昨晚其实做了拉伸", via="telegram")
    assert reminders.get(inst["id"])["done_at"] == "2026-08-14T13:30:00Z"
    assert routines.next_fire(rt["id"]).strftime("%m/%d %H:%M") == "08/16 21:00"
    with pytest.raises(ValueError):
        conversation._past_time("2026-08-16 10:00")                          # 将来的时刻不收

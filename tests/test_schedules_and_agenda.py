from datetime import datetime, timezone

import pytest

from app import clock
from app.scheduler import core
from app.services import meds, reminders
from app.services import schedules as sched_svc


def test_crud_and_validation(fresh_db):
    med = meds.create_med("X")
    row = sched_svc.create("早药", "med", "0 8 * * *", {"med_id": med["id"]})
    assert row["enabled"] == 1
    with pytest.raises(ValueError):
        sched_svc.create("坏cron", "reminder", "99 99 * * *", {})
    with pytest.raises(ValueError):
        sched_svc.create("坏类型", "nope", "0 8 * * *", {})
    with pytest.raises(ValueError):
        sched_svc.create("med缺id", "med", "0 8 * * *", {})
    assert sched_svc.toggle(row["id"])["enabled"] == 0
    assert sched_svc.delete(row["id"]) is True


def test_sync_from_db(fresh_db):
    """schedules 表 → APScheduler job 集合。scheduler 未 start 也可挂 pending job。"""
    med = meds.create_med("Y")
    a = sched_svc.create("a", "med", "0 8 * * *", {"med_id": med["id"]})
    b = sched_svc.create("b", "reminder", "0 9 * * *", {"title": "t"}, enabled=False)
    core.sync_from_db()
    ids = {j.id for j in core.scheduler.get_jobs()}
    assert f"sched:{a['id']}" in ids
    assert f"sched:{b['id']}" not in ids       # 停用的不挂

    sched_svc.toggle(b["id"])                   # 启用 b
    core.sync_from_db()
    ids = {j.id for j in core.scheduler.get_jobs()}
    assert f"sched:{b['id']}" in ids

    sched_svc.delete(a["id"])
    core.sync_from_db()
    ids = {j.id for j in core.scheduler.get_jobs()}
    assert f"sched:{a['id']}" not in ids


def test_upsert_external_dedupe(fresh_db):
    r1 = reminders.upsert_external("买菜", "2026-08-16T10:00:00+09:00")
    assert r1["created"] is True
    # ±2 分钟内同标题:视为同一条
    r2 = reminders.upsert_external("买菜", "2026-08-16T10:01:00+09:00")
    assert r2["created"] is False and r2["row"]["id"] == r1["row"]["id"]
    # 不同时间段:新条目
    r3 = reminders.upsert_external("买菜", "2026-08-16T18:00:00+09:00")
    assert r3["created"] is True


def test_today_agenda(fresh_db):
    # 2026-08-14 12:00 JST = 03:00 UTC
    clock.set_override(datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc))
    med = meds.create_med("Z")
    sched_svc.create("早药", "med", "0 8 * * *", {"med_id": med["id"]})
    sched_svc.create("晚药", "med", "30 21 * * *", {"med_id": med["id"]})
    sched_svc.create("停用的", "reminder", "0 10 * * *", {"title": "no"}, enabled=False)
    reminders.create("倒垃圾", "", "2026-08-14T09:30:00+09:00")
    agenda = reminders.today_agenda()
    times = [(a["time"], a["title"]) for a in agenda]
    assert ("08:00", "早药") in times
    assert ("21:30", "晚药") in times
    assert ("09:30", "倒垃圾") in times
    assert all(a["title"] != "no" for a in agenda)
    assert times == sorted(times, key=lambda x: x[0])


def test_parse_flexible_jst():
    from app import clock

    # 各种快捷指令可能吐出的格式,全部按 JST 解析
    assert clock.parse_flexible_jst("2026-08-24 10:00") == "2026-08-24T01:00:00Z"
    assert clock.parse_flexible_jst("2026/08/24 10:00") == "2026-08-24T01:00:00Z"
    assert clock.parse_flexible_jst("2026年8月24日 10:00") == "2026-08-24T01:00:00Z"
    assert clock.parse_flexible_jst("2026/09/07") == "2026-09-06T15:00:00Z"   # 无时间=0:00 JST
    assert clock.parse_flexible_jst("2026-08-24T10:00:00+09:00") == "2026-08-24T01:00:00Z"

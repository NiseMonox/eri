"""以完成为准每 N 天的 routine(隔天做的拉伸):忘了第二天接着提醒、做完才隔开、
凌晨做完算前一天、提醒前做了也算、今日日程只列该做的日子、时间表替换/停止。"""

from datetime import datetime, timedelta, timezone

import pytest

from app import clock, events
from app.scheduler import jobs
from app.services import reminders, routines


def _at(day: int, hh: int, mm: int = 0) -> None:
    """时钟拨到第 day 天(第 0 天 = 2026-09-01 周二)的东京时间 hh:mm。"""
    local = datetime(2026, 9, 1, hh, mm, tzinfo=clock.TOKYO) + timedelta(days=day)
    clock.set_override(local.astimezone(timezone.utc))


@pytest.fixture()
def quiet(monkeypatch, sent_notices):
    from app.audio import tts

    async def no(*a, **k):
        return False

    monkeypatch.setattr(tts, "announce_routine", no)
    return sent_notices


def _stretch(every: int = 2):
    rt = routines.create("ストレッチ", "care", "20分")
    [s] = routines.set_schedule(rt["id"], ["21:00"], every_days=every)
    return rt, s


async def _fire(s: dict, day: int) -> None:
    _at(day, 21)
    await jobs.run_schedule(s["id"])


def _fired_days(rt: dict) -> list[int]:
    return sorted(clock.to_local(clock.parse_iso(i["due_at"])).day for i in routines.instances(rt["id"]))


async def test_every_other_day_carries_over_until_done(fresh_db, quiet):
    rt, s = _stretch()
    assert s["cron"] == "0 21 * * *" and s["payload"]["every_days"] == 2
    await _fire(s, 0)                                   # 从没做过 → 提醒
    _at(0, 23, 30)
    reminders.sweep_nag()                               # 没理 → 作罢
    assert routines.instances(rt["id"])[0]["status"] == "missed"
    await _fire(s, 1)                                   # 没做完 → 第二天接着提醒
    [today] = routines.today_instances()
    routines.complete(today["id"], via="telegram")
    await _fire(s, 2)                                   # 昨天做完了 → 今天休息
    await _fire(s, 3)                                   # 隔了一天 → 提醒
    assert _fired_days(rt) == [1, 2, 4]
    assert events.recent(kind_prefix="routine_not_due")
    assert not events.recent(kind_prefix="schedule_error")


async def test_after_midnight_completion_counts_for_previous_day(fresh_db, quiet):
    rt, s = _stretch()
    await _fire(s, 0)
    _at(1, 1, 30)                                       # 熬夜到 1:30 才做完
    [inst] = routines.instances(rt["id"])
    routines.complete(inst["id"], via="telegram")
    await _fire(s, 1)                                   # 算第 0 天的份 → 第 1 天休息
    await _fire(s, 2)
    assert _fired_days(rt) == [1, 3]


async def test_done_before_reminder_and_past_done_at(fresh_db, quiet):
    rt, s = _stretch()
    _at(0, 19)
    row = routines.record_done(rt["id"], via="siri")    # 提醒前就做了:补一条完成记录
    assert row["status"] == "done" and row["due_at"] == row["done_at"]
    await _fire(s, 0)                                   # 当天做完了 → 不提醒
    await _fire(s, 1)
    await _fire(s, 2)                                   # 隔了一天 → 提醒
    assert [i["status"] for i in routines.instances(rt["id"])] == ["notified", "done"]
    # 有未关闭实例时直接完成那条、不另起一行;done_at 可以是过去的时刻(20:30 就做完了)
    routines.record_done(rt["id"], via="telegram",
                         done_at=clock.iso(clock.now_utc() - timedelta(minutes=30)))
    assert [i["status"] for i in routines.instances(rt["id"])] == ["done", "done"]
    assert routines.next_fire(rt["id"]).strftime("%m/%d %H:%M") == "09/05 21:00"   # 第 2 天做完 → 第 4 天


def test_today_agenda_lists_interval_routine_only_on_due_days(fresh_db):
    rt, _ = _stretch()

    def titles():
        return [a["title"] for a in reminders.today_agenda()]

    _at(0, 12)
    assert titles() == ["ストレッチ"]              # 从没做过 → 今天该做
    _at(0, 21, 30)
    routines.record_done(rt["id"])
    assert titles() == ["ストレッチ"]              # 当天做完了也照样列着
    _at(1, 12)
    assert titles() == []                               # 休息日
    _at(2, 12)
    assert titles() == ["ストレッチ"]


async def test_manual_run_ignores_interval(fresh_db, quiet):
    """网页「今すぐ1回実行」是测试用的:还没到日子也照样提醒。"""
    rt, s = _stretch()
    _at(0, 20)
    routines.record_done(rt["id"])
    await jobs.run_schedule(s["id"], force=True)
    assert [i["status"] for i in routines.instances(rt["id"])] == ["notified", "done"]


def test_set_schedule_replaces_describes_and_stops(fresh_db):
    rt = routines.create("ビタミン", "habit")
    routines.set_schedule(rt["id"], ["21:00"], every_days=2)
    assert routines.describe(rt["id"]).startswith("2日ごと 21:00(完了した日から")
    a, b = routines.set_schedule(rt["id"], ["21:00", "08:00"])      # 改成每天早晚:复用第一行,新增第二行
    assert "every_days" not in a["payload"] and routines.describe(rt["id"]) == "毎日 08:00・21:00"
    [c] = routines.set_schedule(rt["id"], ["07:30"], weekdays=[1, 4, 7])
    assert c["id"] == a["id"] and c["cron"] == "30 7 * * 0,1,4"
    assert routines.describe(rt["id"]) == "毎週日・月・木 07:30"
    assert [s["enabled"] for s in routines.schedules_of(rt["id"], include_disabled=True)] == [1, 0]
    for bad in (dict(times=["21:00"], every_days=2, weekdays=[1]), dict(times=["25:00"]),
                dict(times=["08:00"], weekdays=[8]), dict(times=[])):
        with pytest.raises(ValueError):
            routines.set_schedule(rt["id"], **bad)
    assert routines.stop_schedules(rt["id"]) == 1
    assert routines.describe(rt["id"]) == "リマインドなし" and routines.next_fire(rt["id"]) is None

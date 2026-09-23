"""量体重的 routine(category=weight):当天称过就不提醒、称了自动完成(作罢后补称也算)、
追催前先同步秤、补同步的旧数据不串到今天、别的 routine 不受影响、通知不挂确认链接。"""

from datetime import datetime, timedelta

import pytest

from app import clock, events
from app.scheduler import jobs
from app.services import routines, weights


def _t(day: int, hh: int, mm: int = 0) -> str:
    """第 day 天(第 0 天 = 2026-09-01 周二)东京时间 hh:mm 的 UTC ISO。"""
    return clock.iso(datetime(2026, 9, 1, hh, mm, tzinfo=clock.TOKYO) + timedelta(days=day))


def _at(day: int, hh: int, mm: int = 0) -> None:
    clock.set_override(clock.parse_iso(_t(day, hh, mm)))


@pytest.fixture()
def silent(monkeypatch, sent_notices):
    """本机真跑着 VOICEVOX:播报一律吞掉。"""
    from app.audio import tts

    async def no(*a, **k):
        return False

    monkeypatch.setattr(tts, "announce", no)
    return sent_notices


def _weigh_routine():
    rt = routines.create("体重測定", "weight")
    [s] = routines.set_schedule(rt["id"], ["08:00"])
    return rt, s


def _statuses(rt: dict) -> list[tuple]:
    return [(i["status"], i["done_via"]) for i in routines.instances(rt["id"])]


async def test_weighed_before_reminder_skips_it(fresh_db, silent):
    rt, s = _weigh_routine()
    _at(0, 7, 20)
    weights.add_weight(62.5, source="withings")               # 起床就上了秤
    assert _statuses(rt) == [("done", "withings")]           # 补一条完成记录(热力图/周报算这天)
    assert routines.next_fire(rt["id"]).strftime("%m/%d %H:%M") == "09/02 08:00"
    _at(0, 8)
    await jobs.run_schedule(s["id"])
    assert len(routines.instances(rt["id"])) == 1 and not silent
    assert events.recent(kind_prefix="routine_already_done")
    _at(1, 8)
    await jobs.run_schedule(s["id"])                         # 第二天没称 → 照常提醒
    assert [i["status"] for i in routines.instances(rt["id"])] == ["notified", "done"]
    assert silent[-1]["title"] == "体重測定の時間" and silent[-1]["url"] is None


async def test_weighing_completes_open_reminder_only(fresh_db, silent):
    rt, s = _weigh_routine()
    med = routines.create("ビタミン", "med", "1錠")
    [ms] = routines.set_schedule(med["id"], ["08:00"])
    _at(0, 8)
    await jobs.run_schedule(s["id"])
    await jobs.run_schedule(ms["id"])
    _at(0, 8, 20)
    weights.add_weight(62.4, _t(0, 8, 15), source="withings")
    [inst] = routines.instances(rt["id"])
    assert (inst["status"], inst["done_via"], inst["done_at"]) == ("done", "withings", _t(0, 8, 15))
    assert routines.instances(med["id"])[0]["status"] == "notified"   # 别的 routine 不受影响
    _at(0, 21)
    weights.add_weight(62.9, source="withings")               # 晚上又称一次:不重复记
    assert len(routines.instances(rt["id"])) == 1


async def test_late_weigh_in_completes_missed_but_old_data_stays_put(fresh_db, silent):
    rt, s = _weigh_routine()
    _at(1, 8)
    await jobs.run_schedule(s["id"])
    _at(1, 10, 30)
    await jobs.reminder_sweeper()                             # 一直没理 → 作罢
    assert _statuses(rt) == [("missed", None)]
    weights.add_weight(63.0, _t(0, 21), source="withings")    # 补同步进来的前一晚的数据
    assert _statuses(rt) == [("missed", None)]
    _at(1, 13)
    weights.add_weight(62.9, source="telegram")               # 中午才称:算补做
    assert _statuses(rt) == [("done", "telegram")]


async def test_scale_synced_right_before_nag_suppresses_it(fresh_db, silent, monkeypatch):
    from app.ingest import withings

    rt, s = _weigh_routine()
    _at(0, 8)
    await jobs.run_schedule(s["id"])
    assert len(silent) == 1
    synced = []

    async def fake_poll(force=False):                         # 08:25 上的秤,常规轮询还没轮到
        synced.append(force)
        weights.add_weight(62.2, _t(0, 8, 25), source="withings")

    monkeypatch.setattr(withings, "poll_if_due", fake_poll)
    _at(0, 8, 31)
    await jobs.reminder_sweeper()
    assert synced == [True] and len(silent) == 1              # 催之前先同步 → 已完成,不再催
    assert _statuses(rt) == [("done", "withings")]

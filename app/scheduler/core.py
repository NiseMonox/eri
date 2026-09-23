"""schedules 表 → APScheduler。MemoryJobStore,表是唯一事实源;
写端点提交后调 sync_from_db() → 「网页改完即生效」。

cron 一律用 croniter 解析(0=周日的标准语义)。不用 APScheduler 的 CronTrigger:
它的 day_of_week 0=周一(官方承认的历史问题)、DOM/DOW 取 AND 而非标准 OR,
会与 today_agenda(croniter)产生「显示周日、实际周一触发」类分歧。"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.date import DateTrigger
from croniter import croniter

from .. import clock, db, events
from . import jobs

scheduler = AsyncIOScheduler(timezone=clock.TOKYO)

# 重启跨过触发点时的补发窗口(秒):错过不超过这个时长的 med/提醒类任务开机补跑一次
CATCHUP_WINDOW_SEC = 3 * 3600
CATCHUP_TYPES = ("routine", "med", "weight_prompt", "reminder")


class CroniterTrigger(BaseTrigger):
    """用 croniter 计算下次触发,保证与 today_agenda / 校验同一引擎同一语义。"""

    __slots__ = ("cron",)

    def __init__(self, cron: str) -> None:
        croniter(cron)  # 提前抛非法表达式
        self.cron = cron

    def get_next_fire_time(self, previous_fire_time, now):
        base = previous_fire_time or now
        return croniter(self.cron, base.astimezone(clock.TOKYO)).get_next(datetime)

    def __str__(self) -> str:  # pragma: no cover
        return f"croniter[{self.cron}]"


def validate_cron(cron: str) -> None:
    """非法 cron 抛 ValueError。"""
    try:
        croniter(cron)
    except Exception as e:
        raise ValueError(f"cron 表达式无效: {e}") from e


def sync_from_db() -> None:
    rows = db.get_db().execute("SELECT id, cron FROM schedules WHERE enabled=1").fetchall()
    want = {f"sched:{r['id']}": r["cron"] for r in rows}
    for job in scheduler.get_jobs():
        if not job.id.startswith("sched:"):
            continue
        if job.id not in want:
            job.remove()
        elif isinstance(job.trigger, CroniterTrigger) and job.trigger.cron == want[job.id]:
            # cron 没变:保留原 job,避免 replace 把「已到点待执行」的触发吞掉
            del want[job.id]
    for jid, cron in want.items():
        scheduler.add_job(
            jobs.run_schedule,
            CroniterTrigger(cron),
            id=jid,
            args=[int(jid.split(":")[1])],
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )


def register_internal() -> None:
    common = dict(replace_existing=True, coalesce=True)
    scheduler.add_job(jobs.reminder_sweeper, "interval", minutes=1, id="int:reminder_sweeper",
                      misfire_grace_time=55, **common)
    scheduler.add_job(jobs.daily_backup, CroniterTrigger("30 4 * * *"), id="int:backup",
                      misfire_grace_time=3600, **common)
    scheduler.add_job(jobs.nas_backup, CroniterTrigger("35 4 * * *"), id="int:nas_backup",
                      misfire_grace_time=3600, **common)
    scheduler.add_job(jobs.weekly_trim, CroniterTrigger("0 5 * * 1"), id="int:trim",
                      misfire_grace_time=3600, **common)
    scheduler.add_job(jobs.memory_consolidate, CroniterTrigger("0 4 * * *"), id="int:memory_consolidate",
                      misfire_grace_time=3600, **common)
    from ..ingest.withings import poll_if_due

    scheduler.add_job(poll_if_due, "interval", minutes=5, id="int:withings",
                      misfire_grace_time=240, **common)


def add_oneshot(type_: str, payload: dict, in_sec: int) -> str:
    run_at = clock.now_local() + timedelta(seconds=in_sec)
    scheduler.add_job(jobs.run_oneshot, DateTrigger(run_date=run_at), args=[type_, payload],
                      misfire_grace_time=60)
    return clock.iso(run_at)


def catchup_missed() -> list[int]:
    """启动时补发:上一个应触发时刻落在 (last_run_at, now] 且距今不超过窗口的
    med/weight_prompt/reminder 任务,各补跑一次。audio/report 不补(重启后放白噪音很怪)。"""
    now = clock.now_local()
    to_run: list[int] = []
    rows = db.get_db().execute(
        "SELECT id, cron, type, last_run_at FROM schedules WHERE enabled=1 AND type IN (?,?,?,?)",
        CATCHUP_TYPES,
    ).fetchall()
    for r in rows:
        try:
            prev = croniter(r["cron"], now).get_prev(datetime)
        except Exception:
            continue
        if (now - prev).total_seconds() > CATCHUP_WINDOW_SEC:
            continue
        last = clock.parse_iso(r["last_run_at"]) if r["last_run_at"] else None
        if last is None or last < prev:  # 都是 tz-aware,跨时区可直接比
            to_run.append(r["id"])
    for sid in to_run:
        events.log("schedule_catchup", None, "schedule", sid)
        scheduler.add_job(jobs.run_schedule, DateTrigger(run_date=now + timedelta(seconds=5)),
                          args=[sid], misfire_grace_time=300)
    return to_run


def start() -> None:
    scheduler.start()
    register_internal()
    sync_from_db()
    catchup_missed()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

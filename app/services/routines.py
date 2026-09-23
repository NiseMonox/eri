"""通用每日 Routine(吃药/健身/护理/习惯…)。定义在 routines 表;
每次「该做了」= reminders 表一行实例(kind=routine),确认/跳过/追催/漏做/补做全部复用 reminders 状态机。
提醒时间表 = routine 类型的 schedules;payload.every_days≥2 时「以完成为准每 N 天」(见 interval_due)。"""

import json
import re
import secrets
from datetime import date, datetime, time, timedelta

from croniter import croniter

from .. import clock, db, events
from . import reminders

# 不带图标:对外文字一律不用 emoji(routines.icon 列是旧数据,读出来也丢掉)
CATEGORIES = {
    "med": {"ja": "お薬", "notify_profile": "med"},
    "exercise": {"ja": "運動", "notify_profile": "info"},
    "care": {"ja": "ケア", "notify_profile": "info"},
    "habit": {"ja": "習慣", "notify_profile": "info"},
    "other": {"ja": "その他", "notify_profile": "info"},
}


# --- 定义 CRUD ---

def create(name: str, category: str = "other", detail: str = "", nag: dict | None = None) -> dict:
    if category not in CATEGORIES:
        category = "other"
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO routines (name, category, detail, nag, created_at) VALUES (?,?,?,?,?)",
        (name, category, detail, json.dumps(nag) if nag else None, clock.now_iso()),
    )
    conn.commit()
    return get(cur.lastrowid)


def get(rid: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
    return _row(row) if row else None


def _row(row) -> dict:
    d = dict(row)
    d["nag"] = json.loads(d["nag"]) if d.get("nag") else None
    d.pop("icon", None)
    return d


def list_all(include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM routines" + ("" if include_inactive else " WHERE active=1") + " ORDER BY id"
    return [_row(r) for r in db.get_db().execute(q).fetchall()]


def update(rid: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "category", "detail", "active", "nag")}
    if "nag" in allowed:
        allowed["nag"] = json.dumps(allowed["nag"]) if allowed["nag"] else None
    if allowed:
        conn = db.get_db()
        sets = ", ".join(f"{k}=?" for k in allowed)
        conn.execute(f"UPDATE routines SET {sets} WHERE id=?", (*allowed.values(), rid))
        conn.commit()
    return get(rid)


# --- 实例(委托 reminders 状态机)---

def create_instance(routine: dict, schedule_id: int | None, nag_override: dict | None = None,
                    force: bool = False) -> dict | None:
    """生成一次「该做 XX 了」实例。去重与 reminders.create_instance 对齐:按 **schedule_id**
    (同一 schedule 还有未关闭实例才跳过,防 misfire 重复);同 routine 的早晚两个 schedule 各自独立。
    force=True 用于 oneshot 测试:不受该去重约束。返回含一次性确认 token 的行。"""
    conn = db.get_db()
    if not force and schedule_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM reminders WHERE schedule_id=? AND status IN ('pending','notified') LIMIT 1",
            (schedule_id,),
        ).fetchone()
        if exists:
            events.log("routine_dedup_skipped", {"routine_id": routine["id"]}, "schedule", schedule_id)
            return None
    nag = nag_override or routine.get("nag")
    cur = conn.execute(
        "INSERT INTO reminders (title, body, due_at, schedule_id, nag, kind, routine_id, token, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (routine["name"], routine.get("detail") or "", clock.now_iso(), schedule_id,
         json.dumps(reminders.nag_params(nag)), "routine", routine["id"],
         secrets.token_urlsafe(16), clock.now_iso()),
    )
    conn.commit()
    events.log("routine_due", {"routine_id": routine["id"], "name": routine["name"]},
               "reminder", cur.lastrowid)
    return reminders.get(cur.lastrowid)


def get_instance_by_token(token: str) -> dict | None:
    row = db.get_db().execute("SELECT * FROM reminders WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def complete(instance_id: int, via: str = "web", done_at: str | None = None) -> dict | None:
    """确认完成(pending/notified/missed → done),记录渠道。幂等。
    done_at=实际完成时刻(「昨晚其实做了」,UTC ISO),缺省=现在——以完成为准的间隔按它算。"""
    row = reminders.get(instance_id)
    if row is None:
        return None
    if row["status"] not in ("pending", "notified", "missed"):
        return row
    conn = db.get_db()
    conn.execute(
        "UPDATE reminders SET status='done', done_at=?, done_via=? "
        "WHERE id=? AND status IN ('pending','notified','missed')",
        (done_at or clock.now_iso(), via, instance_id),
    )
    conn.commit()
    events.log("routine_done", {"via": via, "late": row["status"] == "missed"},
               "reminder", instance_id)
    return reminders.get(instance_id)


def skip(instance_id: int, via: str = "web") -> dict | None:
    """跳过(pending/notified/missed → dismissed):「今日はナシ」对超时的也成立。"""
    row = reminders.get(instance_id)
    if row is None or row["status"] not in ("pending", "notified", "missed"):
        return row
    conn = db.get_db()
    conn.execute(
        "UPDATE reminders SET status='dismissed', done_at=?, done_via=? "
        "WHERE id=? AND status IN ('pending','notified','missed')",
        (clock.now_iso(), via, instance_id),
    )
    conn.commit()
    events.log("routine_skipped", {"via": via, "late": row["status"] == "missed"},
               "reminder", instance_id)
    return reminders.get(instance_id)


def latest_open(category: str | None = None) -> dict | None:
    """最近一条待确认(notified/pending)的 routine 实例;可按分类筛(「薬飲んだ」只找 med)。"""
    q = ("SELECT r.* FROM reminders r JOIN routines t ON t.id=r.routine_id "
         "WHERE r.kind='routine' AND r.status IN ('notified','pending')")
    args: list = []
    if category:
        q += " AND t.category=?"
        args.append(category)
    q += " ORDER BY r.due_at DESC LIMIT 1"
    row = db.get_db().execute(q, args).fetchone()
    return dict(row) if row else None


def instances(routine_id: int | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    q = ("SELECT r.*, t.name AS routine_name, t.category "
         "FROM reminders r JOIN routines t ON t.id=r.routine_id WHERE r.kind='routine'")
    args: list = []
    if routine_id:
        q += " AND r.routine_id=?"
        args.append(routine_id)
    if status:
        q += " AND r.status=?"
        args.append(status)
    q += " ORDER BY r.due_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.get_db().execute(q, args).fetchall()]


def today_instances() -> list[dict]:
    start = clock.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.get_db().execute(
        "SELECT r.*, t.name AS routine_name, t.category "
        "FROM reminders r JOIN routines t ON t.id=r.routine_id WHERE r.kind='routine' AND r.due_at>=? "
        "ORDER BY r.due_at",
        (clock.iso(start),),
    ).fetchall()
    return [dict(r) for r in rows]


# --- 统计 / 热力图 ---

def heatmap(weeks: int = 12) -> dict:
    """{routine_id: {"YYYY-MM-DD": "done|dismissed|missed|pending"}},按东京日期。
    同一天多条取「最差」优先级 missed > pending > dismissed > done?——不,取最新一条的状态更直观。"""
    start_local = (clock.now_local() - timedelta(weeks=weeks)).replace(hour=0, minute=0, second=0,
                                                                        microsecond=0)
    rows = db.get_db().execute(
        "SELECT routine_id, due_at, status FROM reminders WHERE kind='routine' AND due_at>=? "
        "ORDER BY due_at",
        (clock.iso(start_local),),
    ).fetchall()
    out: dict[int, dict[str, str]] = {}
    for r in rows:
        day = clock.to_local(clock.parse_iso(r["due_at"])).strftime("%Y-%m-%d")
        out.setdefault(r["routine_id"], {})[day] = r["status"]   # 后写覆盖 = 当天最新一条
    return out


def completion_rate(routine_id: int, days: int = 7) -> dict:
    cutoff = clock.iso(clock.now_utc() - timedelta(days=days))
    rows = db.get_db().execute(
        "SELECT status, COUNT(*) AS n FROM reminders WHERE kind='routine' AND routine_id=? "
        "AND due_at>=? GROUP BY status",
        (routine_id, cutoff),
    ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    done = counts.get("done", 0)
    return {"done": done, "total": total, "rate": (done / total) if total else None,
            "missed": counts.get("missed", 0), "dismissed": counts.get("dismissed", 0)}


def weekly_summary_ja() -> str:
    parts = []
    for r in list_all():
        c = completion_rate(r["id"], 7)
        if c["total"]:
            parts.append(f"{r['name']} {c['done']}/{c['total']}")
    return "、".join(parts)


# --- 以完成为准的间隔(隔天做的拉伸:忘了第二天接着提醒,做完才隔开)---

# 凌晨 4 点前做完/确认的算前一天的份:熬夜到 1 点才做完,不该让下次提醒晚一天
DAY_START_HOUR = 4
WEEKDAY_JA = "日月火水木金土"   # 下标 = cron 的星期(0=周日)


def habit_day(dt_local: datetime) -> date:
    return (dt_local - timedelta(hours=DAY_START_HOUR)).date()


def habit_day_start(day: date) -> datetime:
    """习惯日的起点(东京时间 day 的 04:00)。记忆整理与对话窗口也按这个边界切天。"""
    return datetime.combine(day, time(DAY_START_HOUR), tzinfo=clock.TOKYO)


def last_done_at(routine_id: int, before: date | None = None) -> str | None:
    """最近一次完成时刻(UTC ISO);before=只看这个习惯日之前的。"""
    q = "SELECT MAX(done_at) FROM reminders WHERE kind='routine' AND routine_id=? AND status='done'"
    args: list = [routine_id]
    if before is not None:
        q += " AND done_at < ?"
        args.append(clock.iso(habit_day_start(before)))
    return db.get_db().execute(q, args).fetchone()[0]


def interval_due(routine_id: int, every_days: int, day: date, planning: bool = False) -> bool:
    """「每 N 天、以完成为准」:距上次完成满 N 天(或从没做过)起,每天都该提醒,直到做完——
    忘了就自然顺延到第二天。planning=True(排今日日程用)只看这天之前的完成:当天做完了也照样列出。"""
    last = last_done_at(routine_id, before=day if planning else None)
    if last is None:
        return True
    return (day - habit_day(clock.to_local(clock.parse_iso(last)))).days >= every_days


def find_by_name(name: str) -> dict | None:
    """同名 routine(优先启用中的),防对话里重复新建。"""
    row = db.get_db().execute(
        "SELECT * FROM routines WHERE name=? ORDER BY active DESC, id LIMIT 1", (name,)
    ).fetchone()
    return _row(row) if row else None


def schedules_of(routine_id: int, include_disabled: bool = False) -> list[dict]:
    from . import schedules as sched_svc

    return [s for s in sched_svc.list_all()
            if s["type"] in ("routine", "med")
            and (s["payload"].get("routine_id") or s["payload"].get("med_id")) == routine_id
            and (include_disabled or s["enabled"])]


def build_crons(times: list[str], every_days: int = 1, weekdays: list[int] | None = None) -> list[str]:
    """校验并生成 cron(每个时刻一条)。weekdays:1=周一…7=周日;every_days≥2 与 weekdays 互斥。"""
    if isinstance(times, str):   # LLM 偶尔把单个时刻直接给成字符串
        times = [times]
    every = int(every_days or 1)
    if every < 1:
        raise ValueError("every_days は 1 以上だよ")
    if every > 1 and weekdays:
        raise ValueError("「N日ごと」と曜日指定は一緒に使えないよ")
    if weekdays and not all(1 <= int(d) <= 7 for d in weekdays):
        raise ValueError("曜日は 1(月)〜7(日)で指定してね")
    dow = ",".join(str(d) for d in sorted({int(d) % 7 for d in weekdays})) if weekdays else "*"
    crons = []
    for t in dict.fromkeys(times or []):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(t))
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"時刻の形式がおかしいよ:{t}")
        crons.append(f"{int(m.group(2))} {int(m.group(1))} * * {dow}")
    if not crons:
        raise ValueError("何時に声かけるか決まってないよ")
    return crons


def set_schedule(routine_id: int, times: list[str], every_days: int = 1,
                 weekdays: list[int] | None = None) -> list[dict]:
    """设定(替换)routine 的提醒时间表,每个时刻一条 schedule。复用已有行改 cron/payload
    (保留 nag 等其他字段),多出来的停用不删——网页上还能手动恢复。"""
    from . import schedules as sched_svc

    routine = get(routine_id)
    if routine is None:
        raise ValueError("そのルーティンは見つからないよ")
    crons = build_crons(times, every_days, weekdays)
    every = int(every_days or 1)
    extra = {"routine_id": routine_id} | ({"every_days": every} if every > 1 else {})
    old = schedules_of(routine_id, include_disabled=True)
    out = []
    for i, cron in enumerate(crons):
        if i < len(old):
            payload = {k: v for k, v in old[i]["payload"].items() if k != "every_days"} | extra
            out.append(sched_svc.update(old[i]["id"], name=routine["name"], type_="routine",
                                        cron=cron, payload=payload, enabled=True))
        else:
            out.append(sched_svc.create(routine["name"], "routine", cron, dict(extra)))
    for s in old[len(crons):]:
        if s["enabled"]:
            sched_svc.update(s["id"], enabled=False)
    return out


def stop_schedules(routine_id: int, via: str = "web") -> int:
    """停掉 routine 的全部提醒(schedule 停用不删),正在追催的实例一并作罢。返回停掉的条数。"""
    from . import schedules as sched_svc

    scheds = schedules_of(routine_id)
    for s in scheds:
        sched_svc.update(s["id"], enabled=False)
    for r in db.get_db().execute(
        "SELECT id FROM reminders WHERE kind='routine' AND routine_id=? AND status IN ('pending','notified')",
        (routine_id,),
    ).fetchall():
        skip(r["id"], via=via)
    return len(scheds)


def record_done(routine_id: int, via: str = "web", done_at: str | None = None) -> dict:
    """「做完了」:有未关闭实例(含 24h 内作罢的)就完成那条;没有(提醒之前就做了)就补一条完成记录——
    以完成为准的间隔靠它重新起算。done_at=实际完成时刻(UTC ISO),缺省=现在。"""
    routine = get(routine_id)
    if routine is None:
        raise ValueError("そのルーティンは見つからないよ")
    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM reminders WHERE kind='routine' AND routine_id=? "
        "AND (status IN ('pending','notified') OR (status='missed' AND due_at >= ?)) "
        "ORDER BY due_at DESC LIMIT 1",
        (routine_id, clock.iso(clock.now_utc() - timedelta(hours=24))),
    ).fetchone()
    if row:
        return complete(row["id"], via=via, done_at=done_at)
    at = done_at or clock.now_iso()
    cur = conn.execute(
        "INSERT INTO reminders (title, body, due_at, status, done_at, done_via, kind, routine_id, token, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (routine["name"], routine.get("detail") or "", at, "done", at, via, "routine", routine_id,
         secrets.token_urlsafe(16), clock.now_iso()),
    )
    conn.commit()
    events.log("routine_done", {"via": via, "adhoc": True}, "reminder", cur.lastrowid)
    return reminders.get(cur.lastrowid)


def _cron_times(cron: str) -> tuple[list[str], list[int] | None] | None:
    """规范 cron(「分 时[,时] * * 周[,周]」)→ (["21:00"], [1, 4] 或 None);别的写法返回 None。"""
    f = cron.split()
    if len(f) != 5 or f[2:4] != ["*", "*"] or not f[0].isdigit():
        return None
    hours, dows = f[1].split(","), f[4].split(",")
    if not all(h.isdigit() for h in hours) or (f[4] != "*" and not all(d.isdigit() for d in dows)):
        return None
    return ([f"{int(h):02d}:{int(f[0]):02d}" for h in hours],
            None if f[4] == "*" else sorted({int(d) % 7 for d in dows}))


def describe(routine_id: int) -> str:
    """提醒设定的日语说明(LLM 上下文 / 工具回复 / 网页共用)。"""
    groups: dict[str, list[str]] = {}
    interval = False
    for s in schedules_of(routine_id):
        every = int(s["payload"].get("every_days") or 1)
        parsed = _cron_times(s["cron"])
        if parsed is None:
            groups.setdefault(f"cron「{s['cron']}」", [])
            continue
        times, dows = parsed
        if every > 1:
            label, interval = f"{every}日ごと", True
        elif dows:
            label = "毎週" + "・".join(WEEKDAY_JA[d] for d in dows)
        else:
            label = "毎日"
        groups.setdefault(label, []).extend(times)
    if not groups:
        return "リマインドなし"
    text = "、".join(f"{k} {'・'.join(sorted(v))}".strip() for k, v in groups.items())
    return text + ("(完了した日から数えて、できなかった日は翌日もまた声かける)" if interval else "")


def next_fire(routine_id: int) -> datetime | None:
    """下次真正会提醒的时刻(东京时间;以完成为准的间隔已计入,假设在那之前不再完成)。没设提醒时 None。"""
    now = clock.now_local()
    last = last_done_at(routine_id)
    last_day = habit_day(clock.to_local(clock.parse_iso(last))) if last else None
    best = None
    for s in schedules_of(routine_id):
        every = int(s["payload"].get("every_days") or 1)
        it = croniter(s["cron"], now)
        for _ in range(500):
            t = it.get_next(datetime)
            if every <= 1 or last_day is None or (habit_day(t) - last_day).days >= every:
                if best is None or t < best:
                    best = t
                break
    return best

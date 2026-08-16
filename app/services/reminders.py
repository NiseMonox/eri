"""一次性提醒 + 提醒实例(可改期/完成/追催)+ 当日待办展开。重复性提醒建 schedules,
触发时经 create_instance 落成实例行——对话层(snooze/done)与追催都操作实例。"""

import json
from datetime import datetime, timedelta

from croniter import croniter

from .. import clock, db, events, store

NAG_DEFAULTS = {"every_min": 30, "max": 3, "grace_min": 120}


def nag_params(override: dict | None) -> dict:
    p = dict(NAG_DEFAULTS)
    for src in (store.get("reminder.nag_defaults", {}) or {}, override or {}):
        if isinstance(src, dict):
            p.update({k: v for k, v in src.items() if k in NAG_DEFAULTS})
    for k, default in NAG_DEFAULTS.items():
        try:
            p[k] = max(int(p[k]), 1)
        except (TypeError, ValueError):
            p[k] = default
    return p


def create(title: str, body: str = "", due_at: str | datetime | None = None) -> dict:
    at = (
        clock.now_iso()
        if due_at is None
        else clock.iso(clock.parse_iso(due_at)) if isinstance(due_at, str) else clock.iso(due_at)
    )
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO reminders (title, body, due_at, created_at) VALUES (?,?,?,?)",
        (title, body, at, clock.now_iso()),
    )
    conn.commit()
    return get(cur.lastrowid)


def get(rid: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_all(status: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM reminders"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY due_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.get_db().execute(q, args).fetchall()]


def due_pending() -> list[dict]:
    rows = db.get_db().execute(
        "SELECT * FROM reminders WHERE status='pending' AND due_at <= ? ORDER BY due_at",
        (clock.now_iso(),),
    ).fetchall()
    return [dict(r) for r in rows]


def set_status(rid: int, status: str) -> dict | None:
    """done/dismissed 只能从未关闭状态迁移(不能覆盖历史行);事件只在真的变更时记。"""
    assert status in ("pending", "notified", "done", "dismissed")
    conn = db.get_db()
    if status in ("done", "dismissed"):
        done_at = clock.now_iso() if status == "done" else None
        cur = conn.execute(
            "UPDATE reminders SET status=?, done_at=? "
            "WHERE id=? AND status IN ('pending','notified','missed')",
            (status, done_at, rid),
        )
    else:
        cur = conn.execute("UPDATE reminders SET status=?, done_at=NULL WHERE id=?",
                           (status, rid))
    conn.commit()
    if status == "done" and cur.rowcount > 0:
        events.log("reminder_done", None, "reminder", rid)
    return get(rid)


def create_instance(title: str, schedule_id: int | None, nag: dict | None = None,
                    body: str = "") -> dict | None:
    """schedule 触发时生成当次实例(due_at=现在)。
    去重只看「同 schedule 是否还有未关闭(pending/notified)实例」——防的是 misfire 重复触发;
    前一实例 done/missed/dismissed 后,同日第二时段(如 cron 0 9,21)照常实例化,
    跨午夜 snooze 的实例关闭后也不会挡住次日触发。"""
    conn = db.get_db()
    if schedule_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM reminders WHERE schedule_id=? AND status IN ('pending','notified') "
            "LIMIT 1",
            (schedule_id,),
        ).fetchone()
        if exists:
            return None
    cur = conn.execute(
        "INSERT INTO reminders (title, body, due_at, schedule_id, nag, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (title, body, clock.now_iso(), schedule_id,
         json.dumps(nag_params(nag)), clock.now_iso()),
    )
    conn.commit()
    return get(cur.lastrowid)


def snooze(rid: int, until_iso: str) -> dict | None:
    """改期:回到 pending,追催计数与首播时刻清零,sweeper 到点自然再触发。
    只允许从 pending/notified/missed 改期(done/dismissed 的历史行不能被复活)。"""
    conn = db.get_db()
    cur = conn.execute(
        "UPDATE reminders SET due_at=?, status='pending', remind_count=0, done_at=NULL, "
        "notified_at=NULL WHERE id=? AND status IN ('pending','notified','missed')",
        (clock.iso(clock.parse_iso(until_iso)), rid),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    events.log("reminder_snoozed", {"until": until_iso}, "reminder", rid)
    return get(rid)


def sweep_nag() -> list[tuple[dict, str]]:
    """对 notified(已首播未响应)按 nag 参数追催,返回 [(row, "renag"|"missed"), ...]。
    时钟从 notified_at(首播时刻)起算——过期很久才首播的行不会「首播即作罢」;
    跳位计数:停机恢复不连环补发(同 meds.sweep)。"""
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM reminders WHERE status='notified'").fetchall()
    now = clock.now_utc()
    actions: list[tuple[dict, str]] = []
    for r in rows:
        row = dict(r)
        p = nag_params(json.loads(row["nag"]) if row["nag"] else None)
        if not row.get("notified_at"):
            # 旧数据/异常路径:以现在为首播基准补记,下一轮再进入追催节奏
            conn.execute("UPDATE reminders SET notified_at=? WHERE id=?",
                         (clock.now_iso(), row["id"]))
            conn.commit()
            continue
        elapsed = now - clock.parse_iso(row["notified_at"])
        if elapsed > timedelta(minutes=p["grace_min"]):
            conn.execute("UPDATE reminders SET status='missed' WHERE id=? AND status='notified'",
                         (row["id"],))
            conn.commit()
            events.log("reminder_missed", None, "reminder", row["id"])
            actions.append((get(row["id"]), "missed"))
        else:
            due_count = int(elapsed / timedelta(minutes=p["every_min"]))
            new_count = min(due_count, p["max"])
            if new_count > row["remind_count"]:
                conn.execute("UPDATE reminders SET remind_count=? WHERE id=?",
                             (new_count, row["id"]))
                conn.commit()
                actions.append((get(row["id"]), "renag"))
    return actions


def done_today() -> list[dict]:
    start = clock.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.get_db().execute(
        "SELECT * FROM reminders WHERE status='done' AND done_at>=? ORDER BY done_at",
        (clock.iso(start),),
    ).fetchall()
    return [dict(r) for r in rows]


def open_items() -> list[dict]:
    """对话上下文用:今天的 pending + 所有 notified(追催中)+ 24h 内的 missed
    (作罢的也留在上下文里,「补做了」才有对象可指)。"""
    end = clock.now_local().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    missed_cutoff = clock.iso(clock.now_utc() - timedelta(hours=24))
    rows = db.get_db().execute(
        "SELECT * FROM reminders WHERE status='notified' "
        "OR (status='pending' AND due_at < ?) "
        "OR (status='missed' AND due_at >= ?) ORDER BY due_at",
        (clock.iso(end), missed_cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_external(title: str, due_at_iso: str) -> dict:
    """来自 Apple 提醒事项的导入:同标题且时间在 ±2 分钟内的(任意状态)视为同一条,不重建。
    防回环靠列表隔离(服务器→Apple 写专用列表,反向只读用户自己的列表),这里的去重是保底。"""
    from datetime import timedelta

    due = clock.parse_iso(due_at_iso)
    lo, hi = clock.iso(due - timedelta(minutes=2)), clock.iso(due + timedelta(minutes=2))
    row = db.get_db().execute(
        "SELECT * FROM reminders WHERE title=? AND due_at BETWEEN ? AND ?",
        (title, lo, hi),
    ).fetchone()
    if row:
        return {"created": False, "row": dict(row)}
    r = create(title, "", due_at_iso)
    events.log("reminder_imported", {"title": title}, "reminder", r["id"])
    return {"created": True, "row": r}


def mark_notified(rid: int) -> None:
    """只从 pending 迁移并记录首播时刻(追催时钟基准):
    sweeper 发通知期间用户标了 done 的话不覆盖。"""
    conn = db.get_db()
    conn.execute(
        "UPDATE reminders SET status='notified', notified_at=? WHERE id=? AND status='pending'",
        (clock.now_iso(), rid),
    )
    conn.commit()


def today_agenda() -> list[dict]:
    """把今天(东京时间)的 med/weight_prompt/reminder 类 schedules 用 croniter 展开,
    加上当日一次性 reminders。输出 [{"time":"08:00","kind":"med","title":...}, ...] 按时间排序。"""
    start = clock.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    items: list[dict] = []

    rows = db.get_db().execute(
        "SELECT * FROM schedules WHERE enabled=1 AND type IN ('routine','med','weight_prompt','reminder')"
    ).fetchall()
    routine_cache: dict[int, dict] = {}
    for r in rows:
        payload = json.loads(r["payload"] or "{}")
        title = payload.get("title") or r["name"]
        icon = None
        kind = "routine" if r["type"] == "med" else r["type"]
        if kind == "routine":
            rid = payload.get("routine_id") or payload.get("med_id")
            if rid:
                if rid not in routine_cache:
                    row = db.get_db().execute("SELECT name, icon FROM routines WHERE id=?", (rid,)).fetchone()
                    routine_cache[rid] = dict(row) if row else {}
                title = routine_cache[rid].get("name") or title
                icon = routine_cache[rid].get("icon")
        # get_next 严格大于基准:退 1 秒让 00:00 整点的任务也进当日清单
        it = croniter(r["cron"], start - timedelta(seconds=1))
        while True:
            t = it.get_next(datetime)
            if t >= end:
                break
            items.append({
                "time": t.strftime("%H:%M"),
                "when": t.strftime("%Y-%m-%d %H:%M"),   # 快捷指令可直接转日期
                "kind": kind,
                "icon": icon,
                "title": title,
                "schedule_id": r["id"],
            })

    for r in db.get_db().execute(
        "SELECT * FROM reminders WHERE status IN ('pending','notified') AND due_at >= ? AND due_at < ? "
        "AND kind='reminder' AND schedule_id IS NULL",
        (clock.iso(start), clock.iso(end)),
    ).fetchall():
        local = clock.to_local(clock.parse_iso(r["due_at"]))
        items.append({
            "time": local.strftime("%H:%M"),
            "when": local.strftime("%Y-%m-%d %H:%M"),
            "kind": "reminder",
            "title": r["title"],
            "reminder_id": r["id"],
        })

    return sorted(items, key=lambda x: x["time"])

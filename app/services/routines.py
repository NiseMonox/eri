"""通用每日 Routine(吃药/健身/护理/习惯…)。定义在 routines 表;
每次「该做了」= reminders 表一行实例(kind=routine),确认/跳过/追催/漏做/补做全部复用 reminders 状态机。"""

import json
import secrets
from datetime import timedelta

from .. import clock, db, events
from . import reminders

CATEGORIES = {
    "med": {"icon": "💊", "ja": "お薬", "notify_profile": "med"},
    "exercise": {"icon": "🏋️", "ja": "運動", "notify_profile": "info"},
    "care": {"icon": "🧴", "ja": "ケア", "notify_profile": "info"},
    "habit": {"icon": "✅", "ja": "習慣", "notify_profile": "info"},
    "other": {"icon": "📌", "ja": "その他", "notify_profile": "info"},
}


# --- 定义 CRUD ---

def create(name: str, category: str = "other", detail: str = "", nag: dict | None = None) -> dict:
    if category not in CATEGORIES:
        category = "other"
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO routines (name, category, detail, icon, nag, created_at) VALUES (?,?,?,?,?,?)",
        (name, category, detail, CATEGORIES[category]["icon"],
         json.dumps(nag) if nag else None, clock.now_iso()),
    )
    conn.commit()
    return get(cur.lastrowid)


def get(rid: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
    return _row(row) if row else None


def _row(row) -> dict:
    d = dict(row)
    d["nag"] = json.loads(d["nag"]) if d.get("nag") else None
    if not d.get("icon"):
        d["icon"] = CATEGORIES.get(d.get("category", "other"), CATEGORIES["other"])["icon"]
    return d


def list_all(include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM routines" + ("" if include_inactive else " WHERE active=1") + " ORDER BY id"
    return [_row(r) for r in db.get_db().execute(q).fetchall()]


def update(rid: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "category", "detail", "active", "nag")}
    if "nag" in allowed:
        allowed["nag"] = json.dumps(allowed["nag"]) if allowed["nag"] else None
    if "category" in allowed:
        allowed["icon"] = CATEGORIES.get(allowed["category"], CATEGORIES["other"])["icon"]
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


def complete(instance_id: int, via: str = "web") -> dict | None:
    """确认完成(pending/notified/missed → done),记录渠道。幂等。"""
    row = reminders.get(instance_id)
    if row is None:
        return None
    if row["status"] not in ("pending", "notified", "missed"):
        return row
    conn = db.get_db()
    conn.execute(
        "UPDATE reminders SET status='done', done_at=?, done_via=? "
        "WHERE id=? AND status IN ('pending','notified','missed')",
        (clock.now_iso(), via, instance_id),
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
    q = ("SELECT r.*, t.name AS routine_name, t.category, COALESCE(t.icon,'📌') AS icon "
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
        "SELECT r.*, t.name AS routine_name, t.category, COALESCE(t.icon,'📌') AS icon "
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
            parts.append(f"{r['icon']}{r['name']} {c['done']}/{c['total']}")
    return "、".join(parts)

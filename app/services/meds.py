"""服药闭环状态机:pending → confirmed / skipped / missed(missed 可补确认)。
web、Bark 短链、Telegram、Siri 全部走这里,保证幂等与一致。"""

import json
import secrets
from datetime import timedelta

from .. import clock, db, events, store

DEFAULTS = {"resend_every_min": 30, "max_resends": 3, "grace_min": 180}


def params_for(schedule_payload: dict | None) -> dict:
    """合并默认 → settings → payload,并把值强制转 int(网页填成 "30" 之类不至于炸掉 sweep)。"""
    p = dict(DEFAULTS)
    for src in (store.get("med.defaults", {}) or {}, schedule_payload or {}):
        if not isinstance(src, dict):
            continue
        p.update({k: v for k, v in src.items() if k in DEFAULTS})
    for k, default in DEFAULTS.items():
        try:
            p[k] = max(int(p[k]), 1)
        except (TypeError, ValueError):
            p[k] = default
    return p


# --- meds CRUD ---

def create_med(name: str, dose: str = "", notes: str = "") -> dict:
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO meds (name, dose, notes, created_at) VALUES (?,?,?,?)",
        (name, dose, notes, clock.now_iso()),
    )
    conn.commit()
    return get_med(cur.lastrowid)


def get_med(med_id: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM meds WHERE id=?", (med_id,)).fetchone()
    return dict(row) if row else None


def list_meds(include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM meds" + ("" if include_inactive else " WHERE active=1") + " ORDER BY id"
    return [dict(r) for r in db.get_db().execute(q).fetchall()]


def update_med(med_id: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "dose", "notes", "active")}
    if allowed:
        conn = db.get_db()
        sets = ", ".join(f"{k}=?" for k in allowed)
        conn.execute(f"UPDATE meds SET {sets} WHERE id=?", (*allowed.values(), med_id))
        conn.commit()
    return get_med(med_id)


# --- 状态机 ---

def create_due_log(
    med_id: int, schedule_id: int | None = None, params: dict | None = None
) -> dict:
    """params 为重发参数快照(来自 schedule payload 或 oneshot payload),缺省用全局默认。"""
    p = params_for(params)
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO med_logs (med_id, schedule_id, due_at, token, params, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            med_id,
            schedule_id,
            clock.now_iso(),
            secrets.token_urlsafe(16),
            json.dumps(p),
            clock.now_iso(),
        ),
    )
    conn.commit()
    events.log("med_due", {"med_id": med_id}, "med_log", cur.lastrowid)
    return get_log(cur.lastrowid)


def get_log(log_id: int) -> dict | None:
    row = db.get_db().execute("SELECT * FROM med_logs WHERE id=?", (log_id,)).fetchone()
    return dict(row) if row else None


def get_log_by_token(token: str) -> dict | None:
    row = db.get_db().execute("SELECT * FROM med_logs WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def confirm(log_id: int | None = None, token: str | None = None, via: str = "web") -> dict | None:
    """pending 和 missed 都可确认(missed=补服);confirmed/skipped 幂等返回不覆盖。"""
    log = get_log(log_id) if log_id else get_log_by_token(token) if token else None
    if log is None:
        return None
    if log["status"] not in ("pending", "missed"):
        return log
    was_missed = log["status"] == "missed"
    conn = db.get_db()
    conn.execute(
        "UPDATE med_logs SET status='confirmed', confirmed_at=?, confirm_via=? "
        "WHERE id=? AND status IN ('pending','missed')",
        (clock.now_iso(), via, log["id"]),
    )
    conn.commit()
    log = get_log(log["id"])
    events.log("med_confirmed", {"via": via, "late": was_missed}, "med_log", log["id"])
    return log


def skip(log_id: int, via: str = "web") -> dict | None:
    log = get_log(log_id)
    if log is None or log["status"] != "pending":
        return log
    conn = db.get_db()
    conn.execute(
        "UPDATE med_logs SET status='skipped', confirmed_at=?, confirm_via=? "
        "WHERE id=? AND status='pending'",
        (clock.now_iso(), via, log_id),
    )
    conn.commit()
    events.log("med_skipped", {"via": via}, "med_log", log_id)
    return get_log(log_id)


def list_logs(status: str | None = None, limit: int = 100) -> list[dict]:
    """含药名。status=missed 即漏服视图。"""
    q = (
        "SELECT l.*, m.name AS med_name, m.dose AS med_dose FROM med_logs l "
        "JOIN meds m ON m.id = l.med_id"
    )
    args: list = []
    if status:
        q += " WHERE l.status=?"
        args.append(status)
    q += " ORDER BY l.due_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.get_db().execute(q, args).fetchall()]


def sweep() -> list[tuple[dict, str]]:
    """对 pending 逐条判定,返回 [(log, "resend"|"missed"), ...],由调用方负责发通知。
    remind_count 直接跳到 floor(elapsed/间隔):停机后重启不会 5 分钟一条地连环补发。"""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT l.*, m.name AS med_name, m.dose AS med_dose "
        "FROM med_logs l JOIN meds m ON m.id=l.med_id WHERE l.status='pending'"
    ).fetchall()
    now = clock.now_utc()
    actions: list[tuple[dict, str]] = []
    for r in rows:
        log = dict(r)
        p = params_for(json.loads(log["params"]) if log["params"] else None)
        elapsed = now - clock.parse_iso(log["due_at"])
        extra = {"med_name": log["med_name"], "med_dose": log["med_dose"]}
        if elapsed > timedelta(minutes=p["grace_min"]):
            conn.execute("UPDATE med_logs SET status='missed' WHERE id=? AND status='pending'",
                         (log["id"],))
            conn.commit()
            events.log("med_missed", {"med_id": log["med_id"]}, "med_log", log["id"])
            actions.append((get_log(log["id"]) | extra, "missed"))
        else:
            due_count = int(elapsed / timedelta(minutes=p["resend_every_min"]))
            new_count = min(due_count, p["max_resends"])
            if new_count > log["remind_count"]:
                conn.execute("UPDATE med_logs SET remind_count=? WHERE id=?",
                             (new_count, log["id"]))
                conn.commit()
                actions.append((get_log(log["id"]) | extra, "resend"))
    return actions

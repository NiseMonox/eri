import json
from datetime import datetime, timedelta

from .. import clock, db, events
from . import routines


class InvalidWeight(ValueError):
    pass


def add_weight(
    weight_kg: float,
    measured_at: datetime | str | None = None,
    source: str = "manual",
    raw: dict | None = None,
) -> dict:
    """入库一条体重。返回 {"created": bool, "row": {...}};UNIQUE(measured_at,source) 幂等去重。"""
    kg = float(weight_kg)
    if not (20 < kg < 300):
        raise InvalidWeight(f"weight out of range: {kg}")
    if measured_at is None:
        at = clock.now_iso()
    elif isinstance(measured_at, str):
        at = clock.iso(clock.parse_iso(measured_at))
    else:
        at = clock.iso(measured_at)

    conn = db.get_db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO weights (measured_at, weight_kg, source, raw, created_at) "
        "VALUES (?,?,?,?,?)",
        (at, kg, source, json.dumps(raw, ensure_ascii=False) if raw else None, clock.now_iso()),
    )
    conn.commit()
    created = cur.rowcount > 0
    row = conn.execute(
        "SELECT * FROM weights WHERE measured_at=? AND source=?", (at, source)
    ).fetchone()
    if created:
        events.log("weight_added", {"kg": kg, "source": source}, "weight", row["id"])
        try:
            routines.weight_recorded(at, source)   # 体重类 routine 的提醒:称了就自动完成
        except Exception as e:  # noqa: BLE001  体重已经入库,联动失败只记日志
            events.log("routine_error", {"weight_link": str(e)[:200]}, "weight", row["id"])
    return {"created": created, "row": dict(row)}


def list_weights(
    from_: str | None = None, to: str | None = None, limit: int = 200
) -> list[dict]:
    q = "SELECT * FROM weights"
    cond, args = [], []
    if from_:
        cond.append("measured_at >= ?")
        args.append(clock.iso(clock.parse_iso(from_)))
    if to:
        cond.append("measured_at <= ?")
        args.append(clock.iso(clock.parse_iso(to)))
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY measured_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.get_db().execute(q, args).fetchall()]


def recent_days(days: int) -> list[dict]:
    cutoff = clock.iso(clock.now_utc() - timedelta(days=days))
    rows = db.get_db().execute(
        "SELECT * FROM weights WHERE measured_at >= ? ORDER BY measured_at", (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete(weight_id: int) -> bool:
    conn = db.get_db()
    cur = conn.execute("DELETE FROM weights WHERE id=?", (weight_id,))
    conn.commit()
    return cur.rowcount > 0


def stats(days: int = 7) -> dict | None:
    rows = recent_days(days)
    if not rows:
        return None
    kgs = [r["weight_kg"] for r in rows]
    return {
        "days": days,
        "count": len(rows),
        "avg": round(sum(kgs) / len(kgs), 2),
        "min": min(kgs),
        "max": max(kgs),
        "first": kgs[0],
        "last": kgs[-1],
        "delta": round(kgs[-1] - kgs[0], 2),
    }

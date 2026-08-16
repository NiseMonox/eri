"""event_log:全系统审计/调试。触发、推送、入库、错误都落这里。"""

import json

from . import clock, db


def log(kind: str, detail: dict | str | None = None, ref_type: str | None = None, ref_id: int | None = None) -> None:
    d = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail
    conn = db.get_db()
    conn.execute(
        "INSERT INTO event_log (ts, kind, ref_type, ref_id, detail) VALUES (?,?,?,?,?)",
        (clock.now_iso(), kind, ref_type, ref_id, d),
    )
    conn.commit()


def recent(limit: int = 50, kind_prefix: str | None = None) -> list:
    conn = db.get_db()
    if kind_prefix:
        rows = conn.execute(
            "SELECT * FROM event_log WHERE kind LIKE ? ORDER BY id DESC LIMIT ?",
            (kind_prefix + "%", limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def trim(days: int = 90) -> int:
    from datetime import timedelta

    cutoff = clock.iso(clock.now_utc() - timedelta(days=days))
    conn = db.get_db()
    cur = conn.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount

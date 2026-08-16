import json

from .. import clock, db

TYPES = ("audio", "med", "weight_prompt", "reminder", "report")


def _validate(type_: str, cron: str, payload: dict) -> None:
    from ..scheduler import core

    if type_ not in TYPES:
        raise ValueError(f"invalid type: {type_}")
    core.validate_cron(cron)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if type_ == "med" and not payload.get("med_id"):
        raise ValueError("med schedule 需要 payload.med_id")


def _row(r) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


def list_all() -> list[dict]:
    rows = db.get_db().execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def get(sid: int) -> dict | None:
    r = db.get_db().execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    return _row(r) if r else None


def create(name: str, type_: str, cron: str, payload: dict, enabled: bool = True) -> dict:
    _validate(type_, cron, payload)
    conn = db.get_db()
    now = clock.now_iso()
    cur = conn.execute(
        "INSERT INTO schedules (name, type, cron, payload, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, type_, cron, json.dumps(payload, ensure_ascii=False), int(enabled), now, now),
    )
    conn.commit()
    _resync()
    return get(cur.lastrowid)


def update(sid: int, *, name=None, type_=None, cron=None, payload=None, enabled=None) -> dict | None:
    cur_row = get(sid)
    if cur_row is None:
        return None
    new_type = type_ if type_ is not None else cur_row["type"]
    new_cron = cron if cron is not None else cur_row["cron"]
    new_payload = payload if payload is not None else cur_row["payload"]
    _validate(new_type, new_cron, new_payload)
    conn = db.get_db()
    conn.execute(
        "UPDATE schedules SET name=?, type=?, cron=?, payload=?, enabled=?, updated_at=? WHERE id=?",
        (
            name if name is not None else cur_row["name"],
            new_type,
            new_cron,
            json.dumps(new_payload, ensure_ascii=False),
            int(enabled if enabled is not None else cur_row["enabled"]),
            clock.now_iso(),
            sid,
        ),
    )
    conn.commit()
    _resync()
    return get(sid)


def delete(sid: int) -> bool:
    conn = db.get_db()
    n = conn.execute("DELETE FROM schedules WHERE id=?", (sid,)).rowcount
    conn.commit()
    _resync()
    return n > 0


def toggle(sid: int) -> dict | None:
    conn = db.get_db()
    conn.execute("UPDATE schedules SET enabled = 1-enabled, updated_at=? WHERE id=?",
                 (clock.now_iso(), sid))
    conn.commit()
    _resync()
    return get(sid)


def _resync() -> None:
    from ..scheduler import core

    if core.scheduler.running:
        core.sync_from_db()

import sqlite3
from pathlib import Path

from .config import settings

_conn: sqlite3.Connection | None = None

SCHEMA_VERSION = 4

# 增量迁移:key=目标版本。schema.sql 永远是 v1 基线,新库=基线+全部迁移,老库=按版本补。
# 每个版本在单一事务内执行并连带写版本号:中途失败整体回滚,不会出现「列已加、版本没动」的启动死循环
MIGRATIONS = {
    2: """
ALTER TABLE reminders ADD COLUMN schedule_id INTEGER;
ALTER TABLE reminders ADD COLUMN remind_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reminders ADD COLUMN nag TEXT;
CREATE TABLE chat_log (
  id   INTEGER PRIMARY KEY,
  ts   TEXT NOT NULL,
  via  TEXT,
  role TEXT NOT NULL,              -- user|assistant
  text TEXT NOT NULL
);
CREATE INDEX idx_chatlog_ts ON chat_log(ts);
""",
    3: """
ALTER TABLE reminders ADD COLUMN notified_at TEXT;
""",
    4: """
CREATE TABLE body_metrics (
  id          INTEGER PRIMARY KEY,
  measured_at TEXT NOT NULL,          -- UTC ISO8601
  metric      TEXT NOT NULL,          -- fat_ratio|fat_mass|muscle_mass|bone_mass|hydration|heart_rate|pwv|visceral_fat|...
  value       REAL NOT NULL,
  unit        TEXT,                   -- % | kg | bpm | m/s | ...
  source      TEXT NOT NULL DEFAULT 'withings',
  raw         TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (measured_at, metric, source)
);
CREATE INDEX idx_bodymetrics_metric_at ON body_metrics(metric, measured_at);
""",
}


def init_db(path: Path | str | None = None) -> sqlite3.Connection:
    """建立(或重建)全局连接并跑迁移。测试传 tmp 路径。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    p = Path(path) if path is not None else settings.db_path
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(p, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA foreign_keys=ON")
    _migrate(_conn)
    return _conn


def get_db() -> sqlite3.Connection:
    if _conn is None:
        return init_db()
    return _conn


def _apply_version(conn: sqlite3.Connection, sql_text: str, target: int) -> None:
    """一个版本 = 一个事务(语句 + user_version 一起提交/回滚)。SQLite 的 DDL 与
    PRAGMA user_version 都是事务性的,失败后库停留在旧版本,可安全重试。"""
    old_iso = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        for stmt in (s.strip() for s in sql_text.split(";")):
            if stmt:
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version={target}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_iso


def _migrate(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v < 1:
        _apply_version(conn, (Path(__file__).parent / "schema.sql").read_text(), 1)
        v = 1
    for target in range(v + 1, SCHEMA_VERSION + 1):
        _apply_version(conn, MIGRATIONS[target], target)


def backup(dest_dir: Path, keep: int = 14) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    from . import clock

    dest = dest_dir / f"healthhub-{clock.now_local().strftime('%Y%m%d')}.db"
    dst = sqlite3.connect(dest)
    with dst:
        get_db().backup(dst)
    dst.close()
    old = sorted(dest_dir.glob("healthhub-*.db"))[:-keep]
    for f in old:
        f.unlink()
    return dest

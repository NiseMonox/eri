import sqlite3
from pathlib import Path

from .config import settings

_conn: sqlite3.Connection | None = None

SCHEMA_VERSION = 6

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
    # v5:meds → 通用 routines;med_logs → reminders 实例(kind=routine);长期记忆表
    5: """
CREATE TABLE routines (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  category   TEXT NOT NULL DEFAULT 'other',   -- med|exercise|care|habit|other
  detail     TEXT,
  icon       TEXT,
  nag        TEXT,                              -- 追催参数 JSON(空=全局默认)
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
ALTER TABLE reminders ADD COLUMN kind TEXT NOT NULL DEFAULT 'reminder';
ALTER TABLE reminders ADD COLUMN routine_id INTEGER;
ALTER TABLE reminders ADD COLUMN token TEXT;
ALTER TABLE reminders ADD COLUMN done_via TEXT;
CREATE INDEX idx_reminders_routine ON reminders(routine_id, due_at);
CREATE UNIQUE INDEX idx_reminders_token ON reminders(token) WHERE token IS NOT NULL;
INSERT INTO routines (id, name, category, detail, icon, active, created_at)
  SELECT id, name, 'med',
         TRIM(COALESCE(dose,'') || CASE WHEN notes IS NOT NULL AND notes<>'' THEN ' / '||notes ELSE '' END),
         '💊', active, created_at FROM meds;
INSERT INTO reminders (title, body, due_at, status, done_at, created_at, schedule_id,
                       remind_count, nag, notified_at, kind, routine_id, token, done_via)
  SELECT m.name, '', l.due_at,
         CASE l.status WHEN 'confirmed' THEN 'done' WHEN 'skipped' THEN 'dismissed'
                       WHEN 'pending' THEN 'notified' ELSE l.status END,
         l.confirmed_at, l.created_at, l.schedule_id, l.remind_count,
         REPLACE(REPLACE(COALESCE(l.params,''), 'resend_every_min', 'every_min'), 'max_resends', 'max'),
         l.due_at, 'routine', l.med_id, l.token, l.confirm_via
  FROM med_logs l JOIN meds m ON m.id = l.med_id;
CREATE TABLE schedules_v5 (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN ('audio','routine','med','weight_prompt','reminder','report')),
  cron        TEXT NOT NULL,
  payload     TEXT NOT NULL DEFAULT '{}',
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
INSERT INTO schedules_v5 SELECT id, name,
  CASE type WHEN 'med' THEN 'routine' ELSE type END,
  cron, REPLACE(payload, '"med_id"', '"routine_id"'), enabled, last_run_at, created_at, updated_at
  FROM schedules;
DROP TABLE schedules;
ALTER TABLE schedules_v5 RENAME TO schedules;
ALTER TABLE meds RENAME TO _legacy_meds;
ALTER TABLE med_logs RENAME TO _legacy_med_logs;
CREATE TABLE memories (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,        -- schedule|preference|fact|mood
  text         TEXT NOT NULL,
  valid_from   TEXT,
  valid_until  TEXT,                 -- 空=长期有效
  source       TEXT NOT NULL DEFAULT 'assistant',
  active       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  last_seen_at TEXT
);
CREATE INDEX idx_memories_active ON memories(active, kind);
""",
    # v6:长期记忆库化(每日整理 + 本地向量 RAG)。memories 只增不删:改写/取代 = 新行 + 旧行 active=0 指向新行;
    # 水位线 memory.watermark = 已整理到的 chat_log.id,初始化为当前最大值 = 从现在开始整理
    6: """
ALTER TABLE chat_log ADD COLUMN actions TEXT;
ALTER TABLE memories ADD COLUMN core INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN event_at TEXT;
ALTER TABLE memories ADD COLUMN day TEXT;
ALTER TABLE memories ADD COLUMN superseded_by INTEGER;
ALTER TABLE memories ADD COLUMN run_id INTEGER;
ALTER TABLE memories ADD COLUMN src_hi INTEGER;
CREATE TABLE memory_vectors (
  memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  vec       BLOB NOT NULL
);
CREATE TABLE memory_runs (
  id          INTEGER PRIMARY KEY,
  day         TEXT,
  lo_id       INTEGER,
  hi_id       INTEGER,
  status      TEXT NOT NULL,
  stats       TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_memories_day ON memories(active, day);
CREATE UNIQUE INDEX idx_memories_diary ON memories(day) WHERE kind='diary' AND active=1;
INSERT OR REPLACE INTO settings (key, value, updated_at)
  SELECT 'memory.watermark', COALESCE(MAX(id), 0), strftime('%Y-%m-%dT%H:%M:%SZ', 'now') FROM chat_log
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
    _migrate(_conn, None if str(p) == ":memory:" else p)
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


def _migrate(conn: sqlite3.Connection, path: Path | None = None) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if path is not None and 1 <= v < SCHEMA_VERSION:
        _pre_migration_backup(conn, path, SCHEMA_VERSION)
    if v < 1:
        _apply_version(conn, (Path(__file__).parent / "schema.sql").read_text(), 1)
        v = 1
    for target in range(v + 1, SCHEMA_VERSION + 1):
        _apply_version(conn, MIGRATIONS[target], target)


def _pre_migration_backup(conn: sqlite3.Connection, path: Path, target: int) -> None:
    """升级前留整库快照 data/backups/pre-v{N}-migration.db。失败只告警不挡升级:迁移本身是事务性的。"""
    dest = path.parent / "backups" / f"pre-v{target}-migration.db"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(dest)
        with dst:
            conn.backup(dst)
        dst.close()
    except Exception as e:  # noqa: BLE001
        print(f"[db] 迁移前备份失败(继续升级): {e}")


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

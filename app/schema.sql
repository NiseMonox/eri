CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,             -- JSON 编码
  updated_at TEXT NOT NULL
);

CREATE TABLE schedules (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN ('audio','med','weight_prompt','reminder','report')),
  cron        TEXT NOT NULL,            -- 5 段 crontab,按 Asia/Tokyo 解释
  payload     TEXT NOT NULL DEFAULT '{}',
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE weights (
  id          INTEGER PRIMARY KEY,
  measured_at TEXT NOT NULL,            -- UTC ISO8601
  weight_kg   REAL NOT NULL CHECK (weight_kg > 20 AND weight_kg < 300),
  source      TEXT NOT NULL DEFAULT 'manual',  -- manual|telegram|hae|withings|siri
  raw         TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (measured_at, source)
);
CREATE INDEX idx_weights_measured ON weights(measured_at);

CREATE TABLE meds (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  dose       TEXT,
  notes      TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE med_logs (
  id           INTEGER PRIMARY KEY,
  med_id       INTEGER NOT NULL REFERENCES meds(id),
  schedule_id  INTEGER,
  due_at       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|missed|skipped
  confirmed_at TEXT,
  confirm_via  TEXT,                    -- bark|telegram|web|siri
  remind_count INTEGER NOT NULL DEFAULT 0,
  token        TEXT UNIQUE,
  params       TEXT,                    -- 创建时的重发参数快照 JSON(resend_every_min/max_resends/grace_min)
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_medlogs_status_due ON med_logs(status, due_at);

CREATE TABLE reminders (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  body       TEXT,
  due_at     TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',   -- pending|notified|done|dismissed
  done_at    TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_reminders_status_due ON reminders(status, due_at);

CREATE TABLE event_log (
  id       INTEGER PRIMARY KEY,
  ts       TEXT NOT NULL,
  kind     TEXT NOT NULL,
  ref_type TEXT,
  ref_id   INTEGER,
  detail   TEXT
);
CREATE INDEX idx_eventlog_ts ON event_log(ts);

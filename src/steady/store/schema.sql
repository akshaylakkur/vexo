-- Steady database schema (SQLite, WAL). See SPEC.md section 8.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS band_sessions (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
  band_no INTEGER NOT NULL, firmware TEXT, transport TEXT, end_reason TEXT
);

CREATE TABLE IF NOT EXISTS connectivity_gaps (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, reason TEXT
);

-- One row every hop (2 s). Spectral magnitudes are band RMS values in the stated units.
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY, session_id INTEGER, t_start REAL NOT NULL, t_end REAL NOT NULL,
  valid INTEGER NOT NULL, valid_frac REAL, n_samples INTEGER, lost_samples INTEGER,
  context TEXT,                                  -- not_worn | walking | rest | active | unknown
  activity_state TEXT, cadence REAL, steps_delta INTEGER,
  acc_dyn_rms REAL,                              -- g
  gyro_rms REAL,                                 -- deg/s (mean removed)
  vol_rms REAL,                                  -- deg/s in 0.5-3 Hz (voluntary movement)
  total_rms REAL,                                -- deg/s in 0.5-15 Hz
  dom_freq REAL, peak_prom REAL, peak_conc REAL, rest_ratio REAL, action_ratio REAL,
  tremor_amp REAL,                               -- deg/s RMS, band-passed 3.5-12 Hz
  dysk_rms REAL, dysk_flat REAL, freeze_index REAL, vigor REAL,
  arm_swing_deg REAL, step_reg REAL, pitch REAL, roll REAL,
  flags INTEGER NOT NULL DEFAULT 0,              -- bit 0 rest tremor, 1 action tremor, 2 dyskinesia, 3 freeze cand., 4 fall cand.
  battery INTEGER, rssi INTEGER
);
CREATE INDEX IF NOT EXISTS windows_t ON windows (t_start);

CREATE TABLE IF NOT EXISTS band_events (
  id INTEGER PRIMARY KEY, session_id INTEGER, t REAL NOT NULL, kind TEXT NOT NULL, arg INTEGER, data_json TEXT
);
CREATE INDEX IF NOT EXISTS band_events_t ON band_events (t);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
  started_at REAL NOT NULL, ended_at REAL, duration_s REAL,
  severity TEXT NOT NULL, metrics_json TEXT, clip_path TEXT, conversation_id INTEGER
);
CREATE INDEX IF NOT EXISTS episodes_t ON episodes (started_at);

CREATE TABLE IF NOT EXISTS hourly_rollups (
  hour_start REAL PRIMARY KEY, windows INTEGER, valid_windows INTEGER,
  worn_s REAL, rest_s REAL, active_s REAL, walking_s REAL,
  rest_tremor_s REAL, action_tremor_s REAL, tremor_pct_rest REAL, tremor_amp_mean REAL,
  dysk_s REAL, vigor_median REAL, steps INTEGER, arm_swing_median REAL, step_reg_median REAL,
  freezes INTEGER, falls INTEGER, valid_frac REAL
);

CREATE TABLE IF NOT EXISTS baselines (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL, kind TEXT NOT NULL,
  value_json TEXT NOT NULL, source TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, trigger TEXT NOT NULL,
  episode_id INTEGER, outcome TEXT, summary TEXT, escalated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, t REAL NOT NULL, role TEXT NOT NULL,
  text TEXT, tool_name TEXT, tool_json TEXT, latency_ms INTEGER
);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL,
  source TEXT, conversation_id INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  type TEXT NOT NULL, tier INTEGER NOT NULL, urgency TEXT NOT NULL, status TEXT NOT NULL,
  payload_json TEXT NOT NULL, openclaw_run_id TEXT, result_note TEXT, attempts INTEGER DEFAULT 0,
  conversation_id INTEGER, episode_id INTEGER
);

-- Every message Steady delivered (or tried to) for an OpenClaw task. M6.
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, t REAL NOT NULL, ok INTEGER NOT NULL,
  channel TEXT, to_role TEXT, by TEXT,                 -- by: openclaw | steady_fallback
  subject TEXT, text TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_task ON deliveries (task_id);

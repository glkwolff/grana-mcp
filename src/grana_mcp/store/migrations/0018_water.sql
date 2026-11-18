-- v18: water per person per day, the daily goal and the reminder settings.
CREATE TABLE water_log (
    id         INTEGER PRIMARY KEY,
    day        TEXT NOT NULL,                  -- YYYY-MM-DD (São Paulo)
    person_id  TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    ml         INTEGER NOT NULL CHECK (ml BETWEEN 1 AND 3000),
    at         TEXT NOT NULL,                  -- when it was logged (UTC ISO)
    by         TEXT
);
CREATE INDEX idx_water_log_day ON water_log (day, person_id);

-- One row per person who changed something; no row = automatic goal, no reminders.
CREATE TABLE water_settings (
    person_id  TEXT PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    goal_ml    INTEGER CHECK (goal_ml BETWEEN 500 AND 8000),   -- NULL: 35 ml per kg
    reminders  INTEGER NOT NULL DEFAULT 0 CHECK (reminders IN (0, 1)),
    start_hour INTEGER NOT NULL DEFAULT 8 CHECK (start_hour BETWEEN 0 AND 23),
    end_hour   INTEGER NOT NULL DEFAULT 22 CHECK (end_hour BETWEEN 1 AND 24),
    every_min  INTEGER NOT NULL DEFAULT 120 CHECK (every_min BETWEEN 30 AND 480),
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    CHECK (end_hour > start_hour)
);

-- v4: the household board shown on the web page (budgets, goals, notices).
-- Edited by the user's MCP client through the board tools; plain data only
-- (the page renders it as text, never as HTML). Amounts are INTEGER cents.

CREATE TABLE board_budgets (
    id                  INTEGER PRIMARY KEY,
    category            TEXT NOT NULL,
    person_id           TEXT REFERENCES people(id) ON DELETE CASCADE,  -- NULL = casa toda
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0),
    note                TEXT,
    updated_at          TEXT NOT NULL,
    updated_by          TEXT
);
CREATE UNIQUE INDEX idx_board_budgets_key ON board_budgets (lower(category), COALESCE(person_id, ''));

CREATE TABLE board_goals (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    saved_cents  INTEGER NOT NULL DEFAULT 0 CHECK (saved_cents >= 0),
    deadline     TEXT,                          -- YYYY-MM-DD
    note         TEXT,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT
);
CREATE UNIQUE INDEX idx_board_goals_title ON board_goals (lower(title));

CREATE TABLE board_notices (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warn', 'ok')),
    expires_on TEXT,                            -- YYYY-MM-DD; hidden after this day
    created_at TEXT NOT NULL,
    created_by TEXT
);

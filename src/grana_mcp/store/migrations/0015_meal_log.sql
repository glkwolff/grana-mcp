-- v15: what each person ate (estimated from a photo, told in the chat, or "the menu's
-- meal"), for the day's calories, streaks and the household's weekly score.
-- Photos are never stored: only the estimate, as text and numbers.
CREATE TABLE meal_log (
    id         INTEGER PRIMARY KEY,
    day        TEXT NOT NULL,                  -- YYYY-MM-DD (São Paulo)
    person_id  TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    meal       TEXT NOT NULL CHECK (meal IN ('cafe', 'almoco', 'lanche', 'jantar', 'extra')),
    descr      TEXT NOT NULL,
    kcal       INTEGER NOT NULL CHECK (kcal BETWEEN 0 AND 6000),
    protein_g  INTEGER CHECK (protein_g BETWEEN 0 AND 400),
    source     TEXT NOT NULL CHECK (source IN ('foto', 'texto', 'cardapio')),
    created_at TEXT NOT NULL,
    by         TEXT
);
CREATE INDEX idx_meal_log_day ON meal_log (day, person_id);

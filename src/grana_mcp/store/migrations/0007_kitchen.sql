-- v7: kitchen (shared diet plan, body metrics, markets and prices).
-- Health data is sensitive (LGPD): it only lives in this local database.

-- One row per person in the diet. Weights in grams, heights in cm (integers).
CREATE TABLE diet_people (
    person_id    TEXT PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    sex          TEXT CHECK (sex IN ('f', 'm')),
    birth_year   INTEGER CHECK (birth_year BETWEEN 1900 AND 2100),
    height_cm    INTEGER CHECK (height_cm BETWEEN 100 AND 250),
    activity     TEXT CHECK (activity IN ('sedentario', 'leve', 'moderado', 'intenso')),
    goal         TEXT CHECK (goal IN ('perder', 'manter', 'ganhar')),
    target_g     INTEGER CHECK (target_g BETWEEN 30000 AND 300000),
    deadline     TEXT,                          -- YYYY-MM-DD
    restrictions TEXT,                          -- free text: "sem lactose"
    likes        TEXT,
    dislikes     TEXT,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT
);

-- Household-level food rules (single row, id = 1).
CREATE TABLE diet_rules (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    weekly_budget_cents  INTEGER CHECK (weekly_budget_cents >= 0),
    delivery_max_week    INTEGER CHECK (delivery_max_week >= 0),
    notes                TEXT,
    home_geohash         TEXT,                  -- ~1 km cell (precision 6), from the browser
    radius_km            INTEGER NOT NULL DEFAULT 5 CHECK (radius_km BETWEEN 1 AND 20),
    updated_at           TEXT NOT NULL,
    updated_by           TEXT
);

CREATE TABLE weight_log (
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    day       TEXT NOT NULL,                    -- YYYY-MM-DD
    grams     INTEGER NOT NULL CHECK (grams BETWEEN 30000 AND 300000),
    by        TEXT,
    PRIMARY KEY (person_id, day)
);

-- "Seguimos o plano hoje?" (household, one answer per day; last answer wins).
CREATE TABLE day_log (
    day      TEXT PRIMARY KEY,
    followed INTEGER NOT NULL CHECK (followed IN (0, 1)),
    note     TEXT,
    by       TEXT
);

-- Weekly shared menu. Portions are for a 2000 kcal reference; each person eats
-- base portions x (their daily target / 2000). Ingredients are per base portion.
CREATE TABLE meal_plan (
    id          INTEGER PRIMARY KEY,
    week_start  TEXT NOT NULL,                  -- Monday, YYYY-MM-DD
    day         INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
    meal        TEXT NOT NULL CHECK (meal IN ('cafe', 'almoco', 'lanche', 'jantar')),
    title       TEXT NOT NULL,
    how         TEXT,                           -- short preparation notes
    kcal        INTEGER CHECK (kcal BETWEEN 0 AND 3000),       -- per base portion
    protein_g   INTEGER CHECK (protein_g BETWEEN 0 AND 300),
    ingredients TEXT NOT NULL,                  -- JSON [{item, qty, unit, search}]
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    UNIQUE (week_start, day, meal)
);

-- Markets the household uses (picked from stores seen in the price source).
CREATE TABLE markets (
    code      TEXT PRIMARY KEY,                 -- price-source store id
    name      TEXT NOT NULL,
    address   TEXT,
    dist_km   REAL,
    favorite  INTEGER NOT NULL DEFAULT 0,
    seen_at   TEXT NOT NULL
);

-- Price observations (from NFC-e based sources). price per package in cents.
CREATE TABLE price_obs (
    id         INTEGER PRIMARY KEY,
    search     TEXT NOT NULL,                   -- normalized search term
    market     TEXT NOT NULL REFERENCES markets(code) ON DELETE CASCADE,
    descr      TEXT NOT NULL,
    gtin       TEXT,
    cents      INTEGER NOT NULL CHECK (cents > 0),
    size       REAL,                            -- package size in base unit (kg, l or un)
    unit       TEXT CHECK (unit IN ('kg', 'l', 'un')),
    sold_at    TEXT NOT NULL,                   -- when it was sold (from the receipt)
    fetched_at TEXT NOT NULL,
    UNIQUE (search, market, descr, sold_at)
);
CREATE INDEX idx_price_obs_search ON price_obs (search, fetched_at);

-- When each search term was last fetched (24 h cache).
CREATE TABLE price_fetch (
    search     TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    results    INTEGER NOT NULL
);

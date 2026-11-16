-- v13: how each phone price lookup ended, so the assistant (and whoever debugs) can see
-- what happened: counts and the stop reason only, never prices or locations.
CREATE TABLE price_lookups (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    user     TEXT,
    total    INTEGER NOT NULL,
    ok       INTEGER NOT NULL,
    offers   INTEGER NOT NULL,
    failed   INTEGER NOT NULL,
    stopped  TEXT
);

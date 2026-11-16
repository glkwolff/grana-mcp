-- v12: Menor Preço lookups now run in the household's phone (their browser, their
-- connection, a few requests at a human pace). The dominant category of each search term
-- barely changes, so it is kept here and the phone skips that request.
CREATE TABLE price_category (
    search     TEXT PRIMARY KEY,
    category   INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

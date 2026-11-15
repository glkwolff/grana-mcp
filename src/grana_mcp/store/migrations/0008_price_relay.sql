-- v8: queue for a price relay (prices fetched from a computer at home). Abandoned: Menor
-- Preço blocks automated access from any IP (scrambled names, fake prices); dropped in v9.
CREATE TABLE price_wanted (
    search    TEXT PRIMARY KEY,
    wanted_at TEXT NOT NULL
);

-- v19: items asked for in chat that are not on the menu ('fralda pampers'), with the
-- detail chosen with the person so the lookup is unambiguous. `search` is exactly the
-- `termo` sent to Menor Preço; `label` is what the person said.
CREATE TABLE price_extra_terms (
    search     TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    gtin       TEXT,
    category   INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT
);
-- Categories left open by the last phone lookup, so the assistant can ask which one.
ALTER TABLE price_lookups ADD COLUMN ambiguous TEXT;

-- v17: every line of the household's receipts (what was bought, how much, for how much), to
-- group the grocery spending by kind of product and follow each product's price. Products
-- are keyed by their folded receipt text; their category is learned once (suggested by the
-- model, confirmed by the household) and reused on every later receipt.
CREATE TABLE receipt_lines (
    id          INTEGER PRIMARY KEY,
    receipt     TEXT NOT NULL REFERENCES receipts(key_hash) ON DELETE CASCADE,
    market      TEXT NOT NULL REFERENCES markets(code) ON DELETE CASCADE,
    day         TEXT NOT NULL,                  -- YYYY-MM-DD of the purchase
    product     TEXT NOT NULL,                  -- folded receipt text ('arroz t1 tio joao 5kg')
    descr       TEXT NOT NULL,                  -- as printed (sanitized, <= 80 chars)
    qty         REAL NOT NULL CHECK (qty > 0),
    unit        TEXT NOT NULL,                  -- as printed: UN, KG, L...
    unit_cents  INTEGER NOT NULL CHECK (unit_cents > 0),
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0)
);
CREATE INDEX idx_receipt_lines_day ON receipt_lines (day);
CREATE INDEX idx_receipt_lines_product ON receipt_lines (product, day);

CREATE TABLE product_categories (
    product    TEXT PRIMARY KEY,                -- receipt_lines.product
    category   TEXT NOT NULL,                   -- a key of kitchen.groceries.CATEGORIES
    status     TEXT NOT NULL CHECK (status IN ('suggested', 'confirmed')),
    by         TEXT,
    updated_at TEXT NOT NULL
);

-- What one package holds (116 diapers, 5 kg, 2 l) when the receipt text doesn't say it
-- plainly, to price what was bought per unit, kg or litre.
CREATE TABLE product_sizes (
    product    TEXT PRIMARY KEY,                -- receipt_lines.product
    size       REAL NOT NULL CHECK (size > 0),
    unit       TEXT NOT NULL CHECK (unit IN ('kg', 'l', 'un')),
    status     TEXT NOT NULL CHECK (status IN ('suggested', 'confirmed')),
    by         TEXT,
    updated_at TEXT NOT NULL
);

-- The bank/card transaction that paid each receipt (same amount, a few days apart), found
-- locally; NULL while not found (paid in cash, a meal voucher, or not synced yet).
ALTER TABLE receipts ADD COLUMN txn_id TEXT REFERENCES transactions(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX idx_receipts_txn ON receipts (txn_id) WHERE txn_id IS NOT NULL;

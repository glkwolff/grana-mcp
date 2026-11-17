-- v16: the household's own NFC-e receipts read from the QR code. Only a salted hash of the
-- receipt key is kept (to skip a receipt already read); its items go to price_obs under
-- an 'nfce:' market (also a salted hash of the store's CNPJ).
CREATE TABLE receipts (
    key_hash    TEXT PRIMARY KEY,
    market      TEXT NOT NULL REFERENCES markets(code) ON DELETE CASCADE,
    day         TEXT NOT NULL,                  -- YYYY-MM-DD of the purchase
    items       INTEGER NOT NULL,
    total_cents INTEGER,
    store       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    by          TEXT
);

-- grana-mcp schema, version 1 (HANDOFF §6).
-- Applied by store/db.py when the database is created. Later changes go in
-- store/migrations/NNNN_<name>.sql (N >= 2); never edit this file after release.
-- Money: INTEGER cents. Internal sign convention: spending / money out = negative.
-- Dates: posted_date is YYYY-MM-DD (America/Sao_Paulo); timestamps are ISO 8601 UTC.

CREATE TABLE profiles (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
    -- credentials are NOT here; they live in the system keyring (key = profile id)
);

CREATE TABLE items (
    id                 TEXT PRIMARY KEY,          -- Pluggy itemId
    profile_id         TEXT NOT NULL REFERENCES profiles(id),
    connector_name     TEXT,
    consent_expires_at TEXT,
    status             TEXT,
    last_synced_at     TEXT
);

CREATE TABLE accounts (
    id            TEXT PRIMARY KEY,               -- Pluggy account id
    item_id       TEXT NOT NULL REFERENCES items(id),
    kind          TEXT NOT NULL CHECK (kind IN ('checking', 'credit_card', 'other')),
    name          TEXT,
    number_masked TEXT,
    currency      TEXT NOT NULL DEFAULT 'BRL'
);

CREATE TABLE transactions (
    id                   TEXT PRIMARY KEY,        -- Pluggy id (or a hash, for OFX)
    account_id           TEXT NOT NULL REFERENCES accounts(id),
    profile_id           TEXT NOT NULL REFERENCES profiles(id),
    posted_date          TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL,        -- spending = negative (INTERNAL convention)
    description_raw      TEXT,
    description_clean    TEXT,
    merchant             TEXT,
    provider_category    TEXT,
    category             TEXT,
    installment_number   INTEGER,
    installment_total    INTEGER,
    counterpart_doc_hash TEXT,                    -- salted SHA-256 of CPF/CNPJ, never plain text
    excluded_reason      TEXT CHECK (excluded_reason IN
                             ('card_bill_payment', 'internal_transfer', 'refund_pair')),
    source               TEXT NOT NULL CHECK (source IN ('pluggy', 'ofx')),
    synced_at            TEXT NOT NULL,
    -- Additions to HANDOFF §6 (raw provider fields needed by the Phase 3 pipeline):
    status               TEXT,                    -- Pluggy POSTED | PENDING
    operation_type       TEXT                     -- Pluggy operationType (e.g. PIX, PAGAMENTO_FATURA)
);

CREATE INDEX idx_transactions_account_date ON transactions (account_id, posted_date);
CREATE INDEX idx_transactions_profile_date ON transactions (profile_id, posted_date);

CREATE TABLE category_rules (
    id         INTEGER PRIMARY KEY,
    pattern    TEXT NOT NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('contains', 'regex', 'merchant')),
    category   TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 0,
    origin     TEXT NOT NULL CHECK (origin IN ('builtin', 'user'))
);

CREATE TABLE household_docs (
    doc_hash   TEXT PRIMARY KEY,                  -- salted SHA-256 of a household CPF
    profile_id TEXT NOT NULL REFERENCES profiles(id)
);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY,
    profile_id  TEXT NOT NULL REFERENCES profiles(id),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
    n_new       INTEGER NOT NULL DEFAULT 0,
    n_updated   INTEGER NOT NULL DEFAULT 0,
    n_deleted   INTEGER NOT NULL DEFAULT 0,        -- addition to HANDOFF §6
    error       TEXT
);

-- v3: people (account ownership), household documents per person, identity
-- of each item (hash only) and the Phase 3 dedupe columns.
--
-- SQLite cannot change a CHECK constraint in place, so `transactions` and
-- `household_docs` are rebuilt (create new, copy, drop old, rename). Nothing
-- references either table, so this is safe with foreign_keys = ON. The whole
-- file runs inside one transaction (store/db.py), so a failure leaves v2 intact.

CREATE TABLE people (
    id           TEXT PRIMARY KEY,                -- slug of the display name
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Who owns each account. NULL = not assigned: the account's profile is used as
-- the person (backward compatible with one-person-per-profile setups).
ALTER TABLE accounts ADD COLUMN owner_person_id TEXT REFERENCES people(id);

-- Holder document of each item (GET /identity), as a salted hash. Nothing else
-- from the identity payload is stored.
CREATE TABLE item_identities (
    item_id    TEXT PRIMARY KEY REFERENCES items(id),
    doc_hash   TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- household_docs: a document may belong to a person (preferred) or, for v2
-- rows and profile-only setups, to a profile.
CREATE TABLE household_docs_v3 (
    doc_hash   TEXT PRIMARY KEY,                  -- salted SHA-256 of a household CPF/CNPJ
    person_id  TEXT REFERENCES people(id),
    profile_id TEXT REFERENCES profiles(id),
    source     TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'identity')),
    created_at TEXT
);
INSERT INTO household_docs_v3 (doc_hash, profile_id, source)
    SELECT doc_hash, profile_id, 'manual' FROM household_docs;
DROP TABLE household_docs;
ALTER TABLE household_docs_v3 RENAME TO household_docs;

CREATE TABLE transactions_v3 (
    id                   TEXT PRIMARY KEY,
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
    counterpart_doc_hash TEXT,
    excluded_reason      TEXT CHECK (excluded_reason IN
                             ('card_bill_payment', 'internal_transfer', 'refund_pair',
                              'investment')),
    source               TEXT NOT NULL CHECK (source IN ('pluggy', 'ofx')),
    synced_at            TEXT NOT NULL,
    status               TEXT,
    operation_type       TEXT,
    counterpart_doc_type TEXT CHECK (counterpart_doc_type IN ('CPF', 'CNPJ')),
    -- Pipeline-owned (pipeline/dedupe.py recomputes them on every run):
    excluded_pair_id     TEXT,                    -- the matched row, when the rule pairs rows
    review_flag          TEXT CHECK (review_flag IN
                             ('unmatched_bill_payment', 'unmatched_refund'))
);
INSERT INTO transactions_v3 (
    id, account_id, profile_id, posted_date, amount_cents, description_raw,
    description_clean, merchant, provider_category, category, installment_number,
    installment_total, counterpart_doc_hash, excluded_reason, source, synced_at, status,
    operation_type, counterpart_doc_type
)
SELECT
    id, account_id, profile_id, posted_date, amount_cents, description_raw,
    description_clean, merchant, provider_category, category, installment_number,
    installment_total, counterpart_doc_hash, excluded_reason, source, synced_at, status,
    operation_type, counterpart_doc_type
FROM transactions;
DROP TABLE transactions;
ALTER TABLE transactions_v3 RENAME TO transactions;

CREATE INDEX idx_transactions_account_date ON transactions (account_id, posted_date);
CREATE INDEX idx_transactions_profile_date ON transactions (profile_id, posted_date);
CREATE INDEX idx_transactions_excluded ON transactions (excluded_reason, posted_date);

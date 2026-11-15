-- v2: closed credit card bills (GET /bills) and the CPF/CNPJ kind of a
-- transaction counterpart (needed to mask third-party natural persons).
-- Amounts are positive INTEGER cents. Dates are YYYY-MM-DD calendar dates.

CREATE TABLE bills (
    id                    TEXT PRIMARY KEY,         -- Pluggy bill id
    account_id            TEXT NOT NULL REFERENCES accounts(id),
    due_date              TEXT NOT NULL,
    closing_date          TEXT,                     -- Pluggy billClosingDate (nullable)
    total_cents           INTEGER NOT NULL,
    currency              TEXT NOT NULL DEFAULT 'BRL',
    minimum_payment_cents INTEGER,
    allows_installments   INTEGER,                  -- 0/1/NULL
    synced_at             TEXT NOT NULL
);

CREATE INDEX idx_bills_account_closing ON bills (account_id, closing_date);

CREATE TABLE bill_payments (
    bill_id      TEXT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,                  -- position in the provider's list
    payment_date TEXT,
    amount_cents INTEGER NOT NULL,
    value_type   TEXT,                              -- FULL_PAYMENT | INSTALLMENT_PAYMENT | ...
    payment_mode TEXT,                              -- PIX | DEBIT_ACCOUNT | BANK_SLIP | ...
    PRIMARY KEY (bill_id, seq)
);

ALTER TABLE transactions ADD COLUMN counterpart_doc_type TEXT
    CHECK (counterpart_doc_type IN ('CPF', 'CNPJ'));

-- v5: phone notifications (Web Push). Subscriptions are created by the web page;
-- the sender runs on the host (`grana-mcp notify run`), where the VAPID key lives.

CREATE TABLE push_subscriptions (
    id         INTEGER PRIMARY KEY,
    user       TEXT NOT NULL,                  -- web user (ana, bia, ...)
    endpoint   TEXT NOT NULL UNIQUE,           -- push service URL (allowlisted hosts)
    p256dh     TEXT NOT NULL,                  -- browser public key (base64url)
    auth       TEXT NOT NULL,                  -- browser auth secret (base64url)
    created_at TEXT NOT NULL,
    last_ok_at TEXT
);

-- One row per alert already delivered (or deliberately skipped), so each fires once.
CREATE TABLE notify_sent (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

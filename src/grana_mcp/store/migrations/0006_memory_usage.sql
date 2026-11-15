-- v6: family memory (short notes the household told the assistant) and chat usage.

-- Facts people told the assistant in the chat ("a Bia recebe dia 5"). Shown on
-- the page, where anyone can delete them. Never filled from transaction text.
CREATE TABLE memory_notes (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE UNIQUE INDEX idx_memory_notes_text ON memory_notes (lower(text));

CREATE VIRTUAL TABLE memory_fts USING fts5(
    text, content='memory_notes', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER memory_notes_ai AFTER INSERT ON memory_notes BEGIN
    INSERT INTO memory_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER memory_notes_ad AFTER DELETE ON memory_notes BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- Tokens spent per chat question (counts only; never the question or the answer).
CREATE TABLE chat_usage (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    user          TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    turns         INTEGER,
    input_tokens  INTEGER,
    cache_read    INTEGER,
    cache_write   INTEGER,
    output_tokens INTEGER,
    duration_ms   INTEGER
);

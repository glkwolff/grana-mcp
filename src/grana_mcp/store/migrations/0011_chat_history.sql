-- v11: chat history. One thread per conversation; its id is also the Claude Code session
-- id, so a conversation resumes after a server restart. Each user only sees their own.
CREATE TABLE chat_threads (
    id         TEXT PRIMARY KEY,
    user       TEXT NOT NULL,
    title      TEXT NOT NULL,
    started    INTEGER NOT NULL DEFAULT 0,      -- Claude Code already has this session
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_chat_threads_user ON chat_threads (user, updated_at);

CREATE TABLE chat_messages (
    id         INTEGER PRIMARY KEY,
    thread     TEXT NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('me', 'bot')),
    text       TEXT NOT NULL,
    meta       TEXT,                            -- JSON: token usage and tool count
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_chat_messages_thread ON chat_messages (thread, id);

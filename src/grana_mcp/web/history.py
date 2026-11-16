"""Chat history: threads and messages per web user, in the local database.

A thread id doubles as the Claude Code session id, so ``--resume`` keeps the
conversation going after a restart. Users only ever see their own threads.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from grana_mcp.privacy.redact import sanitize_text


def now_utc() -> str:
    """Microseconds, so threads touched within the same second still sort right."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


MAX_THREADS = 100  # per user; the oldest go first
MAX_TEXT = 40_000
LIST_LIMIT = 50


def title_of(message: str) -> str:
    first = message.strip().splitlines()[0] if message.strip() else ""
    return sanitize_text(first, 60) or "Conversa"


def create(conn: sqlite3.Connection, user: str, first_message: str) -> str:
    tid = str(uuid.uuid4())
    now = now_utc()
    conn.execute(
        "INSERT INTO chat_threads (id, user, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (tid, user, title_of(first_message), now, now),
    )
    old = conn.execute(
        "SELECT id FROM chat_threads WHERE user = ? ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
        (user, MAX_THREADS),
    ).fetchall()
    conn.executemany("DELETE FROM chat_threads WHERE id = ?", old)
    conn.commit()
    return tid


def get(conn: sqlite3.Connection, user: str, tid: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM chat_threads WHERE id = ? AND user = ?", (tid, user)
    ).fetchone()
    return row


def mark_started(conn: sqlite3.Connection, tid: str) -> None:
    conn.execute("UPDATE chat_threads SET started = 1 WHERE id = ?", (tid,))
    conn.commit()


def add(
    conn: sqlite3.Connection,
    tid: str,
    role: str,
    text: str,
    meta: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = now_utc()
    conn.execute(
        "INSERT INTO chat_messages (thread, role, text, meta, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, role, text[:MAX_TEXT], json.dumps(meta) if meta else None, error, now),
    )
    conn.execute("UPDATE chat_threads SET updated_at = ? WHERE id = ?", (now, tid))
    conn.commit()


def threads(conn: sqlite3.Connection, user: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.id, t.title, t.updated_at, COUNT(m.id) FROM chat_threads t "
        "LEFT JOIN chat_messages m ON m.thread = t.id WHERE t.user = ? "
        "GROUP BY t.id ORDER BY t.updated_at DESC LIMIT ?",
        (user, LIST_LIMIT),
    ).fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2], "messages": r[3]} for r in rows]


def messages(conn: sqlite3.Connection, tid: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT role, text, meta, error FROM chat_messages WHERE thread = ? ORDER BY id", (tid,)
    ).fetchall()
    return [
        {"role": r[0], "text": r[1], "meta": json.loads(r[2]) if r[2] else None, "error": r[3]}
        for r in rows
    ]


def delete(conn: sqlite3.Connection, user: str, tid: str) -> bool:
    cur = conn.execute("DELETE FROM chat_threads WHERE id = ? AND user = ?", (tid, user))
    conn.commit()
    return cur.rowcount > 0

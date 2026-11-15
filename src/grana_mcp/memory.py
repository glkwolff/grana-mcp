"""Family memory: short notes the household told the assistant.

Notes are plain text (sanitized, at most ``MAX_NOTE`` characters), shown on the web
page where anyone can delete them, and included in the household snapshot. The
search uses SQLite FTS5 (accents ignored); no embeddings, nothing leaves the PC.
"""

from __future__ import annotations

import re
import sqlite3

from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

MAX_NOTE = 200
MAX_NOTES = 100
MAX_IN_CONTEXT = 40
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class MemoryError_(ValueError):
    """Invalid note (message is safe to show)."""


def add_note(conn: sqlite3.Connection, text: str, by: str | None = None) -> tuple[int, bool]:
    """Store a note; returns (id, created). The same text twice keeps one note."""
    clean = sanitize_text(text, MAX_NOTE)
    if not clean or len(clean) < 3:
        raise MemoryError_("nota vazia")
    row = conn.execute(
        "SELECT id FROM memory_notes WHERE lower(text) = lower(?)", (clean,)
    ).fetchone()
    if row:
        return int(row[0]), False
    count = conn.execute("SELECT COUNT(*) FROM memory_notes").fetchone()[0]
    if count >= MAX_NOTES:
        raise MemoryError_(f"limite de {MAX_NOTES} notas: apague alguma antes")
    cur = conn.execute(
        "INSERT INTO memory_notes (text, created_at, created_by) VALUES (?, ?, ?)",
        (clean, now_utc(), sanitize_text(by, 32) if by else None),
    )
    conn.commit()
    return int(cur.lastrowid or 0), True


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM memory_notes WHERE id = ?", (int(note_id),))
    conn.commit()
    return cur.rowcount > 0


def list_notes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, text, created_at, created_by FROM memory_notes ORDER BY id DESC"
    ).fetchall()


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    """Notes matching any word of ``query`` (prefix match, accents ignored), best first."""
    words = _WORD_RE.findall(sanitize_text(query, 100) or "")[:8]
    if not words:
        return []
    match = " OR ".join(f'"{w}"*' for w in words)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT n.id, n.text, n.created_at, n.created_by FROM memory_fts f "
        "JOIN memory_notes n ON n.id = f.rowid WHERE memory_fts MATCH ? "
        "ORDER BY bm25(memory_fts) LIMIT ?",
        (match, max(1, min(limit, 20))),
    ).fetchall()

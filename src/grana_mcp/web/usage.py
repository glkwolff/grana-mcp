"""Tokens spent per chat question: counts only, never the text."""

from __future__ import annotations

import sqlite3
from typing import Any

from grana_mcp.store.db import now_utc


def record(
    conn: sqlite3.Connection, user: str, ok: bool, tool_calls: int, usage: dict[str, Any] | None
) -> None:
    u = usage or {}
    conn.execute(
        "INSERT INTO chat_usage (at, user, ok, tool_calls, turns, input_tokens, cache_read, "
        "cache_write, output_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_utc(), user, int(ok), tool_calls, u.get("turns"), u.get("input"),
            u.get("cache_read"), u.get("cache_write"), u.get("output"), u.get("ms"),
        ),
    )  # fmt: skip
    conn.commit()


def context_tokens(usage: dict[str, Any] | None) -> int:
    """Everything the model read (new input + cache writes + cache reads)."""
    u = usage or {}
    return sum(int(u.get(k) or 0) for k in ("input", "cache_read", "cache_write"))


def summary(conn: sqlite3.Connection, days: int = 7) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT substr(at, 1, 10) AS day, user, COUNT(*) AS questions, "
        "ROUND(AVG(tool_calls), 1) AS tools, "
        "CAST(AVG(COALESCE(input_tokens,0) + COALESCE(cache_write,0)) AS INTEGER) AS fresh, "
        "CAST(AVG(COALESCE(cache_read,0)) AS INTEGER) AS cached, "
        "CAST(AVG(COALESCE(output_tokens,0)) AS INTEGER) AS output, "
        "CAST(AVG(duration_ms) / 1000 AS INTEGER) AS seconds "
        "FROM chat_usage WHERE at >= date('now', ?) GROUP BY day, user ORDER BY day, user",
        (f"-{max(days, 1)} days",),
    ).fetchall()

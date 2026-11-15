"""Household board: budgets per category, savings goals and notices.

Plain data edited through MCP tools and shown on the web page as text. All
free text is sanitized and length-limited here; money is ``int`` cents.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from grana_mcp.analytics import queries
from grana_mcp.analytics.periods import Period
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

MAX_TITLE = 60
MAX_NOTE = 140
MAX_NOTICE = 200
MAX_ITEMS = 30
LEVELS = ("info", "warn", "ok")


class BoardError(ValueError):
    """Invalid board input (message is safe to show)."""


def _text(value: str | None, limit: int, field: str, *, required: bool = False) -> str | None:
    clean = sanitize_text(value, limit) if value else None
    if required and not clean:
        raise BoardError(f"{field} não pode ser vazio")
    return clean


def _cents(value: float | int, field: str) -> int:
    """Reais (as the model sends them) -> cents, rejecting nonsense."""
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError):
        raise BoardError(f"{field} inválido") from None
    if cents < 0 or cents > 100_000_000_00:
        raise BoardError(f"{field} fora do intervalo")
    return cents


def _date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise BoardError(f"{field} deve ser AAAA-MM-DD") from None


def _person(conn: sqlite3.Connection, ref: str | None) -> str | None:
    if not ref:
        return None
    row = conn.execute(
        "SELECT id FROM people WHERE id = lower(?) OR lower(display_name) = lower(?)",
        (ref.strip(), ref.strip()),
    ).fetchone()
    if row is None:
        raise BoardError(f"pessoa '{sanitize_text(ref, 32)}' não existe")
    return str(row[0])


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


# --- writes ---


def set_budget(
    conn: sqlite3.Connection,
    category: str,
    monthly_limit: float,
    person: str | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> str:
    """Create/update a monthly budget. ``monthly_limit`` 0 removes it."""
    cat = _text(category, 40, "categoria", required=True)
    person_id = _person(conn, person)
    cents = _cents(monthly_limit, "limite")
    key = (cat or "").lower(), person_id or ""
    if cents == 0:
        cur = conn.execute(
            "DELETE FROM board_budgets WHERE lower(category) = ? AND COALESCE(person_id, '') = ?",
            key,
        )
        conn.commit()
        return "removido" if cur.rowcount else "não existia"
    exists = conn.execute(
        "SELECT id FROM board_budgets WHERE lower(category) = ? AND COALESCE(person_id, '') = ?",
        key,
    ).fetchone()
    values = (cents, _text(note, MAX_NOTE, "nota"), now_utc(), actor)
    if exists:
        conn.execute(
            "UPDATE board_budgets SET monthly_limit_cents = ?, note = ?, updated_at = ?, "
            "updated_by = ? WHERE id = ?",
            (*values, exists[0]),
        )
    else:
        if _count(conn, "board_budgets") >= MAX_ITEMS:
            raise BoardError(f"máximo de {MAX_ITEMS} orçamentos")
        conn.execute(
            "INSERT INTO board_budgets (category, person_id, monthly_limit_cents, note, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
            (cat, person_id, *values),
        )
    conn.commit()
    return "atualizado" if exists else "criado"


def set_goal(
    conn: sqlite3.Connection,
    title: str,
    target: float | None = None,
    saved: float | None = None,
    add_saved: float | None = None,
    deadline: str | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> str:
    """Create/update a goal by title. ``add_saved`` adds to what was saved."""
    name = _text(title, MAX_TITLE, "título", required=True)
    row = conn.execute(
        "SELECT id, target_cents, saved_cents, deadline, note FROM board_goals "
        "WHERE lower(title) = lower(?)",
        (name,),
    ).fetchone()
    if row is None and target is None:
        raise BoardError("informe o valor alvo (target) para criar o objetivo")
    target_c = _cents(target, "alvo") if target is not None else int(row[1])
    if target_c == 0:
        raise BoardError("alvo deve ser maior que zero")
    saved_c = _cents(saved, "guardado") if saved is not None else (int(row[2]) if row else 0)
    if add_saved is not None:
        saved_c += round(float(add_saved) * 100)
    saved_c = max(saved_c, 0)
    dl = _date(deadline, "prazo") if deadline is not None else (row[3] if row else None)
    nt = _text(note, MAX_NOTE, "nota") if note is not None else (row[4] if row else None)
    if row:
        conn.execute(
            "UPDATE board_goals SET target_cents = ?, saved_cents = ?, deadline = ?, note = ?, "
            "updated_at = ?, updated_by = ? WHERE id = ?",
            (target_c, saved_c, dl, nt, now_utc(), actor, row[0]),
        )
    else:
        if _count(conn, "board_goals") >= MAX_ITEMS:
            raise BoardError(f"máximo de {MAX_ITEMS} objetivos")
        conn.execute(
            "INSERT INTO board_goals (title, target_cents, saved_cents, deadline, note, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, target_c, saved_c, dl, nt, now_utc(), actor),
        )
    conn.commit()
    return "atualizado" if row else "criado"


def delete_goal(conn: sqlite3.Connection, title: str) -> bool:
    cur = conn.execute("DELETE FROM board_goals WHERE lower(title) = lower(?)", (title.strip(),))
    conn.commit()
    return cur.rowcount > 0


def post_notice(
    conn: sqlite3.Connection,
    text: str,
    level: str = "info",
    expires_on: str | None = None,
    actor: str | None = None,
) -> int:
    body = _text(text, MAX_NOTICE, "aviso", required=True)
    if level not in LEVELS:
        raise BoardError(f"level deve ser um de {', '.join(LEVELS)}")
    if _count(conn, "board_notices") >= MAX_ITEMS:
        raise BoardError(f"máximo de {MAX_ITEMS} avisos; remova algum")
    cur = conn.execute(
        "INSERT INTO board_notices (text, level, expires_on, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (body, level, _date(expires_on, "expira_em"), now_utc(), actor),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def delete_notice(conn: sqlite3.Connection, notice_id: int) -> bool:
    cur = conn.execute("DELETE FROM board_notices WHERE id = ?", (int(notice_id),))
    conn.commit()
    return cur.rowcount > 0


# --- read ---


@dataclass(frozen=True)
class BudgetStatus:
    category: str
    person: str | None
    limit_cents: int
    spent_cents: int
    note: str | None

    @property
    def pct(self) -> float:
        return round(100 * self.spent_cents / self.limit_cents, 1)


def month_period(today: date) -> Period:
    return Period(start=today.replace(day=1), end=today, label=today.strftime("%Y-%m"))


def budgets(conn: sqlite3.Connection, today: date) -> list[BudgetStatus]:
    period = month_period(today)
    out = []
    rows = conn.execute(
        "SELECT b.category, b.person_id, pe.display_name, b.monthly_limit_cents, b.note "
        "FROM board_budgets b LEFT JOIN people pe ON pe.id = b.person_id "
        "ORDER BY lower(b.category), b.person_id"
    ).fetchall()
    for r in rows:
        filters = queries.Filters(people=[r[1]]) if r[1] else None
        spent = {
            b.key.lower(): b.cents
            for b in queries.spending_by(conn, period, today, "category", filters)
        }
        out.append(BudgetStatus(r[0], r[2], int(r[3]), spent.get(str(r[0]).lower(), 0), r[4]))
    return out


def goals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT title, target_cents, saved_cents, deadline, note, updated_at, updated_by "
        "FROM board_goals ORDER BY deadline IS NULL, deadline, lower(title)"
    ).fetchall()


def notices(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, text, level, expires_on, created_at, created_by FROM board_notices "
        "WHERE expires_on IS NULL OR expires_on >= ? ORDER BY created_at DESC",
        (today.isoformat(),),
    ).fetchall()

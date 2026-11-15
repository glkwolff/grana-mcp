"""SQL aggregations used by the MCP tools. Read-only; money stays ``int`` cents.

Spending = rows with ``amount_cents < 0`` and ``excluded_reason IS NULL``, dated
inside the period and not after ``today`` (future-dated rows are scheduled card
installments, reported only by ``card_bills``). Amounts returned here are
positive cents of spending.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from grana_mcp.analytics.card_bill import CardTx, ClosedBill
from grana_mcp.analytics.periods import Period
from grana_mcp.privacy.redact import PERSON_LABEL, looks_like_person_transfer, sanitize_text

GroupBy = Literal["category", "merchant", "profile", "account"]
FALLBACK_CATEGORY = "Outros"
UNKNOWN_MERCHANT = "(sem descrição)"


def category_sql(alias: str = "t") -> str:
    """The single place that decides a transaction's category.

    Phase 2: the local pipeline category (always NULL until Phase 4), else the
    Pluggy category (English), else "Outros". Phase 4 replaces this.
    """
    return (
        f"COALESCE(NULLIF({alias}.category, ''), NULLIF({alias}.provider_category, ''), "
        f"'{FALLBACK_CATEGORY}')"
    )


# --- merchant name fallback (until the Phase 3 normalizer exists) ---

_INSTALLMENT_RE = re.compile(
    r"(\s*-\s*parcela\s*\d+\s*/\s*\d+|\s+parc(ela)?\.?\s*\d+\s*/\s*\d+|\s+\d{1,2}\s*/\s*\d{1,2})\s*$",
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(r"^(pg|mp|pag|ec|ifd|pagseguro|sumup|stone)\s*\*\s*", re.IGNORECASE)
_TRAILING_NUM_RE = re.compile(r"[\s\-*#]+\d{3,}\s*$")


def clean_description(text: str | None) -> str:
    """Best-effort merchant from a description: drop installment suffixes, acquirer
    prefixes and trailing reference numbers; normalize case and spaces."""
    value = sanitize_text(text) or ""
    value = _INSTALLMENT_RE.sub("", value)
    value = _PREFIX_RE.sub("", value)
    value = _TRAILING_NUM_RE.sub("", value)
    value = " ".join(value.split())
    return value.upper() if value else UNKNOWN_MERCHANT


def household_hashes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT doc_hash FROM household_docs")}


def merchant_label(row: sqlite3.Row, household: set[str]) -> str:
    """Display merchant for a transaction row (masked for third-party persons)."""
    description = row["description_raw"]
    if looks_like_person_transfer(
        description,
        counterpart_doc_hash=row["counterpart_doc_hash"],
        counterpart_doc_type=row["counterpart_doc_type"],
        operation_type=row["operation_type"],
        household_hashes=household,
    ):
        return PERSON_LABEL
    merchant = row["merchant"] or row["description_clean"]
    if merchant:
        return (sanitize_text(merchant) or UNKNOWN_MERCHANT).upper()
    return clean_description(description)


def account_label(name: str | None, number_masked: str | None) -> str:
    label = " ".join(x for x in (name, number_masked) if x) or "(conta)"
    return sanitize_text(label) or "(conta)"


# --- spending ---


def _spending_where(
    period: Period, today: date, profiles: Sequence[str] | None
) -> tuple[str, list[object]]:
    end = min(period.end, today)
    sql = (
        "t.amount_cents < 0 AND t.excluded_reason IS NULL "
        "AND t.posted_date >= ? AND t.posted_date <= ?"
    )
    params: list[object] = [period.start.isoformat(), end.isoformat()]
    if profiles:
        sql += f" AND t.profile_id IN ({', '.join('?' * len(profiles))})"
        params.extend(profiles)
    return sql, params


@dataclass(frozen=True, slots=True)
class Bucket:
    key: str
    cents: int  # positive spending
    count: int


def spending_total(
    conn: sqlite3.Connection, period: Period, today: date, profiles: Sequence[str] | None = None
) -> Bucket:
    where, params = _spending_where(period, today, profiles)
    row = conn.execute(
        f"SELECT COALESCE(-SUM(t.amount_cents), 0), COUNT(*) FROM transactions t WHERE {where}",  # noqa: S608
        params,
    ).fetchone()
    return Bucket("total", int(row[0]), int(row[1]))


def spending_by(
    conn: sqlite3.Connection,
    period: Period,
    today: date,
    group_by: GroupBy,
    profiles: Sequence[str] | None = None,
) -> list[Bucket]:
    """All buckets, largest spending first (callers cap and fold the tail)."""
    where, params = _spending_where(period, today, profiles)
    if group_by == "merchant":
        return _spending_by_merchant(conn, where, params)
    if group_by == "category":
        key_sql, join = category_sql(), ""
    elif group_by == "profile":
        key_sql = "COALESCE(p.display_name, t.profile_id)"
        join = "LEFT JOIN profiles p ON p.id = t.profile_id"
    elif group_by == "account":
        key_sql = "a.id"
        join = "LEFT JOIN accounts a ON a.id = t.account_id"
    else:
        raise ValueError(f"unknown group_by: {group_by}")
    rows = conn.execute(
        f"SELECT {key_sql} AS k, -SUM(t.amount_cents) AS cents, COUNT(*) AS n "  # noqa: S608
        f"FROM transactions t {join} WHERE {where} GROUP BY k",
        params,
    ).fetchall()
    if group_by == "account":
        labels = {
            r["id"]: account_label(r["name"], r["number_masked"])
            for r in conn.execute("SELECT id, name, number_masked FROM accounts")
        }
        buckets = [Bucket(labels.get(r["k"], "(conta)"), r["cents"], r["n"]) for r in rows]
    else:
        buckets = [Bucket(sanitize_text(str(r["k"])) or "?", r["cents"], r["n"]) for r in rows]
    return sorted(buckets, key=lambda b: (-b.cents, b.key))


def _spending_by_merchant(
    conn: sqlite3.Connection, where: str, params: list[object]
) -> list[Bucket]:
    household = household_hashes(conn)
    totals: dict[str, list[int]] = {}
    for row in conn.execute(
        "SELECT t.amount_cents, t.merchant, t.description_clean, t.description_raw, "  # noqa: S608
        "t.counterpart_doc_hash, t.counterpart_doc_type, t.operation_type "
        f"FROM transactions t WHERE {where}",
        params,
    ):
        acc = totals.setdefault(merchant_label(row, household), [0, 0])
        acc[0] -= row["amount_cents"]
        acc[1] += 1
    buckets = [Bucket(k, v[0], v[1]) for k, v in totals.items()]
    return sorted(buckets, key=lambda b: (-b.cents, b.key))


def possible_double_count(
    conn: sqlite3.Connection, period: Period, today: date, profiles: Sequence[str] | None = None
) -> Bucket:
    """Spending rows Pluggy tagged as card bill payments or same-person transfers.

    Until the Phase 3 dedupe marks them ``excluded_reason``, they are counted.
    """
    where, params = _spending_where(period, today, profiles)
    row = conn.execute(
        f"SELECT COALESCE(-SUM(t.amount_cents), 0), COUNT(*) FROM transactions t WHERE {where} "  # noqa: S608
        "AND (lower(t.provider_category) IN ('credit card payment', 'same person transfer') "
        "OR t.operation_type = 'PAGAMENTO_FATURA')",
        params,
    ).fetchone()
    return Bucket("possible_double_count", int(row[0]), int(row[1]))


# --- individual rows ---


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_rows(
    conn: sqlite3.Connection,
    period: Period,
    *,
    query: str | None,
    category: str | None,
    profiles: Sequence[str] | None,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    """Matching rows (newest first, at most ``limit``) and the total match count.

    Includes excluded rows and future-dated rows (flagged in the output).
    """
    sql = "t.posted_date >= ? AND t.posted_date <= ?"
    params: list[object] = [period.start.isoformat(), period.end.isoformat()]
    if query:
        pattern = f"%{_like_escape(query.lower())}%"
        sql += (
            " AND (lower(COALESCE(t.description_raw, '')) LIKE ? ESCAPE '\\' "
            "OR lower(COALESCE(t.merchant, '')) LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    if category:
        sql += f" AND lower({category_sql()}) = ?"
        params.append(category.strip().lower())
    if profiles:
        sql += f" AND t.profile_id IN ({', '.join('?' * len(profiles))})"
        params.extend(profiles)
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {sql}", params).fetchone()[0]  # noqa: S608
    )
    rows = conn.execute(
        f"SELECT t.*, {category_sql()} AS cat, a.name AS account_name, "  # noqa: S608
        "a.number_masked AS account_number, COALESCE(p.display_name, t.profile_id) AS profile "
        "FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN profiles p ON p.id = t.profile_id "
        f"WHERE {sql} ORDER BY t.posted_date DESC, t.id LIMIT ?",
        [*params, limit],
    ).fetchall()
    return rows, total


# --- cards ---


def card_accounts(conn: sqlite3.Connection, account: str | None = None) -> list[sqlite3.Row]:
    """Credit card accounts; ``account`` filters by id or by a name/number substring."""
    rows = conn.execute(
        "SELECT a.id, a.name, a.number_masked, i.profile_id FROM accounts a "
        "LEFT JOIN items i ON i.id = a.item_id WHERE a.kind = 'credit_card' ORDER BY a.name, a.id"
    ).fetchall()
    if not account:
        return rows
    needle = account.strip().lower()
    return [
        r
        for r in rows
        if r["id"] == account.strip()
        or needle in f"{r['name'] or ''} {r['number_masked'] or ''}".lower()
    ]


def card_transactions(conn: sqlite3.Connection, account_id: str) -> list[CardTx]:
    return [
        CardTx(
            id=r["id"],
            posted_date=date.fromisoformat(r["posted_date"]),
            amount_cents=r["amount_cents"],
            operation_type=r["operation_type"],
            provider_category=r["provider_category"],
        )
        for r in conn.execute(
            "SELECT id, posted_date, amount_cents, operation_type, provider_category "
            "FROM transactions WHERE account_id = ?",
            (account_id,),
        )
    ]


def closed_bills(conn: sqlite3.Connection, account_id: str) -> list[ClosedBill]:
    return [
        ClosedBill(
            id=r["id"],
            closing_date=date.fromisoformat(r["closing_date"]) if r["closing_date"] else None,
            due_date=date.fromisoformat(r["due_date"]),
            total_cents=r["total_cents"],
        )
        for r in conn.execute(
            "SELECT id, closing_date, due_date, total_cents FROM bills WHERE account_id = ? "
            "ORDER BY due_date DESC",
            (account_id,),
        )
    ]


def bill_payments_total(conn: sqlite3.Connection, bill_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0), COUNT(*) FROM bill_payments WHERE bill_id = ?",
        (bill_id,),
    ).fetchone()
    return int(row[0]), int(row[1])

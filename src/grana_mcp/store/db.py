"""SQLite access: connection, migrations, upserts and the queries used by ``status``.

The database file is created with mode 0600 inside a 0700 directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path

from grana_mcp.config import ensure_private_dir
from grana_mcp.providers.base import Account, Bill, Item, Transaction

LATEST_VERSION = 2  # bump together with a new store/migrations/NNNN_*.sql
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Provider-owned transaction columns. Re-syncs overwrite only these; columns
# owned by the local pipeline (description_clean, category, excluded_reason)
# are never touched by a sync.
_TX_PROVIDER_COLUMNS = (
    "account_id",
    "profile_id",
    "posted_date",
    "amount_cents",
    "description_raw",
    "merchant",
    "provider_category",
    "installment_number",
    "installment_total",
    "counterpart_doc_hash",
    "counterpart_doc_type",
    "status",
    "operation_type",
)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _create_private_file(path: Path) -> None:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` and apply migrations."""
    ensure_private_dir(path.parent)
    _create_private_file(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("grana_mcp.store.migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema up to date. Each step is atomic. Returns the final version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    steps: list[tuple[int, str]] = []
    if schema_version(conn) == 0:
        baseline = resources.files("grana_mcp.store").joinpath("schema.sql")
        steps.append((1, baseline.read_text(encoding="utf-8")))
    steps.extend(_migrations())
    for version, sql in steps:
        if version <= schema_version(conn):
            continue
        conn.executescript(
            f"BEGIN;\n{sql}\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({version}, '{now_utc()}');\n"
            "COMMIT;"
        )
    return schema_version(conn)


# --- per-install salt for CPF/CNPJ hashes ---


def load_or_create_salt(directory: Path) -> bytes:
    """Random per-install salt, stored in ``<data dir>/salt`` with mode 0600."""
    ensure_private_dir(directory)
    path = directory / "salt"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    os.chmod(path, 0o600)
    return bytes.fromhex(path.read_text().strip())


def hash_doc(digits: str, salt: bytes) -> str:
    """Salted SHA-256 of a CPF/CNPJ (digits only)."""
    return hashlib.sha256(salt + digits.encode("ascii")).hexdigest()


def doc_type(digits: str | None) -> str | None:
    """``CPF`` (11 digits) or ``CNPJ`` (14 digits). Not sensitive; kept in clear."""
    if digits is None:
        return None
    return {11: "CPF", 14: "CNPJ"}.get(len(digits))


# --- writes ---


@dataclass
class UpsertCounts:
    new: int = 0
    updated: int = 0


class Store:
    def __init__(self, conn: sqlite3.Connection, salt: bytes) -> None:
        self.conn = conn
        self._salt = salt

    def upsert_profile(self, profile_id: str, display_name: str) -> None:
        self.conn.execute(
            "INSERT INTO profiles (id, display_name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name",
            (profile_id, display_name, now_utc()),
        )

    def upsert_item(self, profile_id: str, item: Item) -> None:
        consent = item.consent_expires_at.isoformat() if item.consent_expires_at else None
        self.conn.execute(
            "INSERT INTO items (id, profile_id, connector_name, consent_expires_at, status) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "profile_id = excluded.profile_id, connector_name = excluded.connector_name, "
            "consent_expires_at = excluded.consent_expires_at, status = excluded.status",
            (item.id, profile_id, item.connector_name, consent, item.status),
        )

    def mark_item_synced(self, item_id: str) -> None:
        self.conn.execute("UPDATE items SET last_synced_at = ? WHERE id = ?", (now_utc(), item_id))

    def upsert_account(self, account: Account) -> None:
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, kind, name, number_masked, currency) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "item_id = excluded.item_id, kind = excluded.kind, name = excluded.name, "
            "number_masked = excluded.number_masked, currency = excluded.currency",
            (
                account.id,
                account.item_id,
                account.kind,
                account.name,
                account.number_masked,
                account.currency,
            ),
        )

    def upsert_transactions(
        self, profile_id: str, transactions: Iterable[Transaction]
    ) -> UpsertCounts:
        """Insert or update by provider id. Re-syncing never duplicates rows."""
        counts = UpsertCounts()
        synced_at = now_utc()
        cols = ", ".join(_TX_PROVIDER_COLUMNS)
        for tx in transactions:
            values = (
                tx.account_id,
                profile_id,
                tx.posted_date.isoformat(),
                tx.amount_cents,
                tx.description_raw,
                tx.merchant,
                tx.provider_category,
                tx.installment_number,
                tx.installment_total,
                hash_doc(tx.counterpart_doc, self._salt) if tx.counterpart_doc else None,
                doc_type(tx.counterpart_doc),
                tx.status,
                tx.operation_type,
            )
            existing = self.conn.execute(
                f"SELECT {cols} FROM transactions WHERE id = ?",  # noqa: S608 - constant columns
                (tx.id,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    f"INSERT INTO transactions (id, {cols}, source, synced_at) "  # noqa: S608
                    f"VALUES (?, {', '.join('?' * len(values))}, 'pluggy', ?)",
                    (tx.id, *values, synced_at),
                )
                counts.new += 1
            elif tuple(existing) != values:
                assignments = ", ".join(f"{c} = ?" for c in _TX_PROVIDER_COLUMNS)
                self.conn.execute(
                    f"UPDATE transactions SET {assignments}, synced_at = ? WHERE id = ?",  # noqa: S608
                    (*values, synced_at, tx.id),
                )
                counts.updated += 1
        return counts

    def upsert_bills(self, bills: Iterable[Bill]) -> UpsertCounts:
        """Insert or update closed bills by provider id; payments are replaced per bill."""
        counts = UpsertCounts()
        synced_at = now_utc()
        for bill in bills:
            values = (
                bill.account_id,
                bill.due_date.isoformat(),
                bill.closing_date.isoformat() if bill.closing_date else None,
                bill.total_cents,
                bill.currency,
                bill.minimum_payment_cents,
                None if bill.allows_installments is None else int(bill.allows_installments),
            )
            payments = [
                (
                    i,
                    p.payment_date.isoformat() if p.payment_date else None,
                    p.amount_cents,
                    p.value_type,
                    p.payment_mode,
                )
                for i, p in enumerate(bill.payments)
            ]
            existing = self.conn.execute(
                "SELECT account_id, due_date, closing_date, total_cents, currency, "
                "minimum_payment_cents, allows_installments FROM bills WHERE id = ?",
                (bill.id,),
            ).fetchone()
            old_payments = [
                tuple(r)
                for r in self.conn.execute(
                    "SELECT seq, payment_date, amount_cents, value_type, payment_mode "
                    "FROM bill_payments WHERE bill_id = ? ORDER BY seq",
                    (bill.id,),
                )
            ]
            if existing is None:
                self.conn.execute(
                    "INSERT INTO bills (id, account_id, due_date, closing_date, total_cents, "
                    "currency, minimum_payment_cents, allows_installments, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (bill.id, *values, synced_at),
                )
                counts.new += 1
            elif tuple(existing) != values or old_payments != payments:
                self.conn.execute(
                    "UPDATE bills SET account_id = ?, due_date = ?, closing_date = ?, "
                    "total_cents = ?, currency = ?, minimum_payment_cents = ?, "
                    "allows_installments = ?, synced_at = ? WHERE id = ?",
                    (*values, synced_at, bill.id),
                )
                counts.updated += 1
            else:
                continue
            self.conn.execute("DELETE FROM bill_payments WHERE bill_id = ?", (bill.id,))
            self.conn.executemany(
                "INSERT INTO bill_payments (bill_id, seq, payment_date, amount_cents, "
                "value_type, payment_mode) VALUES (?, ?, ?, ?, ?, ?)",
                [(bill.id, *p) for p in payments],
            )
        return counts

    def delete_missing(self, account_id: str, since: date, keep_ids: set[str]) -> int:
        """Delete rows of ``account_id`` dated on/after ``since`` that the provider no
        longer returns (Pluggy deletes and re-creates card transactions with new ids
        around a bill closing, see docs/pluggy-mapping.md)."""
        rows = self.conn.execute(
            "SELECT id FROM transactions WHERE account_id = ? AND posted_date >= ?",
            (account_id, since.isoformat()),
        ).fetchall()
        stale = [r["id"] for r in rows if r["id"] not in keep_ids]
        self.conn.executemany("DELETE FROM transactions WHERE id = ?", [(i,) for i in stale])
        return len(stale)

    def count_transactions(self, account_id: str, since: date) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE account_id = ? AND posted_date >= ?",
            (account_id, since.isoformat()),
        ).fetchone()
        return int(row[0])

    def start_run(self, profile_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (profile_id, started_at, status) VALUES (?, ?, 'running')",
            (profile_id, now_utc()),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        n_new: int = 0,
        n_updated: int = 0,
        n_deleted: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, n_new = ?, n_updated = ?, "
            "n_deleted = ?, error = ? WHERE id = ?",
            (now_utc(), status, n_new, n_updated, n_deleted, error, run_id),
        )
        self.conn.commit()

    # --- reads for `status` ---

    def items(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.*, p.display_name FROM items i JOIN profiles p ON p.id = i.profile_id "
            "ORDER BY p.id, i.id"
        ).fetchall()

    def last_run(self, profile_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM sync_runs WHERE profile_id = ? ORDER BY id DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        return row

    def account_counts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.id, a.item_id, a.kind, a.name, a.number_masked, COUNT(t.id) AS n "
            "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
            "GROUP BY a.id ORDER BY a.item_id, a.kind, a.name"
        ).fetchall()

    def monthly_spending(self, since: date) -> list[sqlite3.Row]:
        """Per month and account: sum of outflows (negative amounts), raw.

        No dedupe yet (Phase 3): a card bill paid from a checking account shows up
        both as card purchases and as the checking-account payment.
        """
        return self.conn.execute(
            "SELECT substr(t.posted_date, 1, 7) AS month, a.id AS account_id, a.name, a.kind, "
            "SUM(t.amount_cents) AS spent_cents, COUNT(*) AS n "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.amount_cents < 0 AND t.excluded_reason IS NULL AND t.posted_date >= ? "
            "GROUP BY month, a.id ORDER BY month DESC, a.name",
            (since.isoformat(),),
        ).fetchall()

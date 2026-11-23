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
from typing import Literal

from grana_mcp.config import ensure_private_dir
from grana_mcp.providers.base import Account, Bill, Item, Transaction

LATEST_VERSION = 19  # bump together with a new store/migrations/NNNN_*.sql
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Provider-owned transaction columns. Re-syncs overwrite only these; columns
# owned by the local pipeline (description_clean, category, excluded_reason,
# excluded_pair_id, review_flag) are never touched by a sync.
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
        self.salt = salt

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
        self,
        profile_id: str,
        transactions: Iterable[Transaction],
        source: Literal["pluggy", "ofx"] = "pluggy",
    ) -> UpsertCounts:
        """Insert or update by provider id. Re-syncing never duplicates rows.

        ``source='ofx'`` marks rows imported from a file (OFX or CSV)."""
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
                hash_doc(tx.counterpart_doc, self.salt) if tx.counterpart_doc else None,
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
                    f"VALUES (?, {', '.join('?' * len(values))}, ?, ?)",
                    (tx.id, *values, source, synced_at),
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
        """Per month and account: sum of outflows (negative amounts), without the rows
        the dedupe pipeline excluded (bill payments, internal transfers, investments,
        refund pairs)."""
        return self.conn.execute(
            "SELECT substr(t.posted_date, 1, 7) AS month, a.id AS account_id, a.name, a.kind, "
            "SUM(t.amount_cents) AS spent_cents, COUNT(*) AS n "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.amount_cents < 0 AND t.excluded_reason IS NULL AND t.posted_date >= ? "
            "GROUP BY month, a.id ORDER BY month DESC, a.name",
            (since.isoformat(),),
        ).fetchall()


# --- people, ownership and household documents (v3) ---


class AmbiguousAccountError(LookupError):
    """More than one account matches a reference."""


_ALNUM_RE = re.compile(r"[^0-9a-z]")


def _alnum(value: str | None) -> str:
    return _ALNUM_RE.sub("", (value or "").lower())


class People:
    """People, account ownership and household documents. Never stores a plain document."""

    def __init__(self, conn: sqlite3.Connection, salt: bytes) -> None:
        self.conn = conn
        self._salt = salt

    def add(self, person_id: str, display_name: str) -> None:
        """Raises ``sqlite3.IntegrityError`` when ``person_id`` already exists."""
        self.conn.execute(
            "INSERT INTO people (id, display_name, created_at) VALUES (?, ?, ?)",
            (person_id, display_name, now_utc()),
        )

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT pe.id, pe.display_name, COUNT(a.id) AS n_accounts, "
            "(SELECT COUNT(*) FROM household_docs h WHERE h.person_id = pe.id) AS n_docs "
            "FROM people pe LEFT JOIN accounts a ON a.owner_person_id = pe.id "
            "GROUP BY pe.id ORDER BY pe.display_name, pe.id"
        ).fetchall()

    def find(self, ref: str) -> sqlite3.Row | None:
        """By id or display name (case-insensitive)."""
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM people WHERE id = ? OR lower(display_name) = lower(?) "
            "ORDER BY id = ? DESC LIMIT 1",
            (ref.strip(), ref.strip(), ref.strip()),
        ).fetchone()
        return row

    def accounts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.id, a.item_id, a.kind, a.name, a.number_masked, a.owner_person_id, "
            "pe.display_name AS owner_name, i.profile_id, pr.display_name AS profile_name "
            "FROM accounts a LEFT JOIN people pe ON pe.id = a.owner_person_id "
            "LEFT JOIN items i ON i.id = a.item_id LEFT JOIN profiles pr ON pr.id = i.profile_id "
            "ORDER BY a.item_id, a.kind, a.name, a.id"
        ).fetchall()

    def find_account(self, ref: str) -> sqlite3.Row | None:
        """By exact id, id prefix (>= 6 chars) or the end of the masked number.

        Raises ``AmbiguousAccountError`` when several accounts match.
        """
        ref = ref.strip()
        rows = self.accounts()
        exact = [r for r in rows if r["id"] == ref]
        if exact:
            return exact[0]
        # The masked number as typed (with punctuation) first, then digits/letters only.
        typed = ref.lstrip("*")
        matches = [r for r in rows if typed and (r["number_masked"] or "").endswith(typed)]
        if len(matches) != 1:
            suffix = _alnum(ref)
            matches = [
                r
                for r in rows
                if (len(ref) >= 6 and r["id"].startswith(ref))
                or (suffix and _alnum(r["number_masked"]).endswith(suffix))
            ]
        if len(matches) > 1:
            raise AmbiguousAccountError(ref)
        return matches[0] if matches else None

    def assign(self, account_id: str, person_id: str | None) -> None:
        self.conn.execute(
            "UPDATE accounts SET owner_person_id = ? WHERE id = ?", (person_id, account_id)
        )
        self._attach_identity_docs()

    def add_doc(self, digits: str, *, person_id: str | None, profile_id: str | None = None) -> bool:
        """Store the salted hash of a household CPF/CNPJ. Returns False if it existed
        (its owner is updated)."""
        doc_hash = hash_doc(digits, self._salt)
        existed = (
            self.conn.execute(
                "SELECT 1 FROM household_docs WHERE doc_hash = ?", (doc_hash,)
            ).fetchone()
            is not None
        )
        self.conn.execute(
            "INSERT INTO household_docs (doc_hash, person_id, profile_id, source, created_at) "
            "VALUES (?, ?, ?, 'manual', ?) ON CONFLICT(doc_hash) DO UPDATE SET "
            "person_id = excluded.person_id, profile_id = excluded.profile_id, "
            "source = 'manual'",
            (doc_hash, person_id, profile_id, now_utc()),
        )
        return not existed

    def has_item_identity(self, item_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM item_identities WHERE item_id = ?", (item_id,)
            ).fetchone()
            is not None
        )

    def set_item_identity(self, item_id: str, digits: str, profile_id: str) -> None:
        """Record the item holder's document (hash only) and add it to the household."""
        doc_hash = hash_doc(digits, self._salt)
        self.conn.execute(
            "INSERT INTO item_identities (item_id, doc_hash, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET doc_hash = excluded.doc_hash, "
            "fetched_at = excluded.fetched_at",
            (item_id, doc_hash, now_utc()),
        )
        # Never overrides a manual entry.
        self.conn.execute(
            "INSERT OR IGNORE INTO household_docs (doc_hash, profile_id, source, created_at) "
            "VALUES (?, ?, 'identity', ?)",
            (doc_hash, profile_id, now_utc()),
        )
        self._attach_identity_docs()

    def _attach_identity_docs(self) -> None:
        """Identity docs without a person get the owner of their item's accounts,
        when all of that item's accounts belong to the same person."""
        self.conn.execute(
            "UPDATE household_docs SET person_id = ("
            "  SELECT MIN(a.owner_person_id) FROM item_identities ii "
            "  JOIN accounts a ON a.item_id = ii.item_id "
            "  WHERE ii.doc_hash = household_docs.doc_hash "
            "  HAVING COUNT(*) = COUNT(a.owner_person_id) "
            "     AND COUNT(DISTINCT a.owner_person_id) = 1"
            ") WHERE source = 'identity' AND person_id IS NULL"
        )

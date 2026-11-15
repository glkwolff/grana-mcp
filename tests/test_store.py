from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from grana_mcp import config
from grana_mcp.store import db


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_db_file_is_private() -> None:
    conn = db.connect(config.db_path())
    conn.close()
    assert _mode(config.db_path()) == 0o600
    assert _mode(config.data_dir()) == 0o700


def test_existing_db_permissions_are_tightened() -> None:
    path = config.db_path()
    path.parent.mkdir(parents=True)
    path.touch(mode=0o644)
    db.connect(path).close()
    assert _mode(path) == 0o600


def test_migrations_create_all_tables_and_are_idempotent() -> None:
    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == db.LATEST_VERSION == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= {
        "schema_version",
        "profiles",
        "items",
        "accounts",
        "transactions",
        "category_rules",
        "household_docs",
        "sync_runs",
        "bills",
        "bill_payments",
    }
    conn.close()
    conn = db.connect(config.db_path())
    assert db.migrate(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 2


def test_salt_is_private_and_stable() -> None:
    salt = db.load_or_create_salt(config.data_dir())
    assert len(salt) == 32
    assert _mode(config.data_dir() / "salt") == 0o600
    assert db.load_or_create_salt(config.data_dir()) == salt


def test_doc_hash_is_salted() -> None:
    a = db.hash_doc("00000000000", b"salt-a")
    assert a == db.hash_doc("00000000000", b"salt-a")
    assert a != db.hash_doc("00000000000", b"salt-b")
    assert len(a) == 64


def test_numbered_migrations_apply_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    db.connect(config.db_path()).close()
    real = db._migrations()
    monkeypatch.setattr(
        db,
        "_migrations",
        lambda: [*real, (3, "ALTER TABLE profiles ADD COLUMN note TEXT;")],
    )
    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 3
    conn.execute("UPDATE profiles SET note = 'x'")
    assert db.migrate(conn) == 3  # not re-applied


def test_migration_v1_to_v2_keeps_existing_data(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build a v1 database (baseline only), with a row in each parent table.
    # monkeypatch.context(), never monkeypatch.undo(): undo would also revert the
    # GRANA_MCP_HOME isolation set up in conftest.
    with monkeypatch.context() as m:
        m.setattr(db, "_migrations", lambda: [])
        conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 1
    conn.executescript(
        "INSERT INTO profiles VALUES ('ana', 'Ana', '2026-01-01T00:00:00+00:00');"
        "INSERT INTO items (id, profile_id) VALUES ('item-1', 'ana');"
        "INSERT INTO accounts (id, item_id, kind) VALUES ('acc-1', 'item-1', 'credit_card');"
        "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
        "source, synced_at) VALUES ('tx-1', 'acc-1', 'ana', '2026-10-01', -100, 'pluggy', 'x');"
    )
    conn.commit()
    conn.close()

    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 2
    row = conn.execute("SELECT amount_cents, counterpart_doc_type FROM transactions").fetchone()
    assert tuple(row) == (-100, None)
    conn.execute(
        "INSERT INTO bills (id, account_id, due_date, total_cents, synced_at) "
        "VALUES ('bill-1', 'acc-1', '2026-11-10', 100, 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET counterpart_doc_type = 'RG'")

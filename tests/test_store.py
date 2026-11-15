from __future__ import annotations

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
    assert db.schema_version(conn) == 1
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
    }
    conn.close()
    conn = db.connect(config.db_path())
    assert db.migrate(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


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
    monkeypatch.setattr(
        db,
        "_migrations",
        lambda: [(2, "ALTER TABLE profiles ADD COLUMN note TEXT;")],
    )
    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 2
    conn.execute("UPDATE profiles SET note = 'x'")
    assert db.migrate(conn) == 2  # not re-applied

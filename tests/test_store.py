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
    assert db.schema_version(conn) == db.LATEST_VERSION == 15
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
        "people",
        "item_identities",
        "board_budgets",
        "board_goals",
        "board_notices",
        "push_subscriptions",
        "notify_sent",
        "memory_notes",
        "chat_usage",
        "diet_people",
        "meal_plan",
        "price_obs",
        "price_lookups",
        "shop_places",
        "item_brands",
        "item_units",
        "meal_log",
    }
    conn.close()
    conn = db.connect(config.db_path())
    assert db.migrate(conn) == 15
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 15


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
        lambda: [*real, (99, "ALTER TABLE profiles ADD COLUMN note TEXT;")],
    )
    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 99
    conn.execute("UPDATE profiles SET note = 'x'")
    assert db.migrate(conn) == 99  # not re-applied


def test_migration_v1_to_latest_keeps_existing_data(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert db.schema_version(conn) == db.LATEST_VERSION
    row = conn.execute("SELECT amount_cents, counterpart_doc_type FROM transactions").fetchone()
    assert tuple(row) == (-100, None)
    conn.execute(
        "INSERT INTO bills (id, account_id, due_date, total_cents, synced_at) "
        "VALUES ('bill-1', 'acc-1', '2026-11-10', 100, 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET counterpart_doc_type = 'RG'")


def test_migration_v2_to_v3_rebuilds_tables_keeping_data(monkeypatch: pytest.MonkeyPatch) -> None:
    real = db._migrations()
    with monkeypatch.context() as m:
        m.setattr(db, "_migrations", lambda: [x for x in real if x[0] <= 2])
        conn = db.connect(config.db_path())
    assert db.schema_version(conn) == 2
    conn.executescript(
        "INSERT INTO profiles VALUES ('ana', 'Ana', '2026-01-01T00:00:00+00:00');"
        "INSERT INTO items (id, profile_id) VALUES ('item-1', 'ana');"
        "INSERT INTO accounts (id, item_id, kind, name) VALUES "
        "('acc-1', 'item-1', 'checking', 'Conta'), ('acc-2', 'item-1', 'credit_card', 'Cartao');"
        "INSERT INTO household_docs VALUES ('hash-1', 'ana');"
        "INSERT INTO bills (id, account_id, due_date, total_cents, synced_at) "
        "VALUES ('bill-1', 'acc-2', '2026-11-10', 100, 'x');"
        "INSERT INTO bill_payments (bill_id, seq, amount_cents) VALUES ('bill-1', 0, 100);"
        "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
        "description_raw, description_clean, merchant, provider_category, category, "
        "installment_number, installment_total, counterpart_doc_hash, excluded_reason, source, "
        "synced_at, status, operation_type, counterpart_doc_type) VALUES "
        "('tx-1', 'acc-1', 'ana', '2026-10-01', -100, 'PIX', 'pix', 'M', 'Transfers', 'Cat', "
        "NULL, NULL, 'hash-x', 'refund_pair', 'pluggy', 's', 'POSTED', 'PIX', 'CPF'),"
        "('tx-2', 'acc-2', 'ana', '2026-10-02', -300, 'LOJA', NULL, NULL, NULL, NULL, "
        "2, 3, NULL, NULL, 'pluggy', 's', 'PENDING', NULL, NULL);"
    )
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
    conn.close()

    conn = db.connect(config.db_path())
    assert db.schema_version(conn) == db.LATEST_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    after = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    old_cols = cols[: len(before[0])]
    assert [tuple(r[c] for c in old_cols) for r in after] == before
    assert cols[-2:] == ["excluded_pair_id", "review_flag"]
    doc = conn.execute("SELECT * FROM household_docs").fetchone()
    assert (doc["doc_hash"], doc["profile_id"], doc["person_id"], doc["source"]) == (
        "hash-1",
        "ana",
        None,
        "manual",
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_transactions_account_date", "idx_transactions_profile_date"} <= indexes
    assert conn.execute("SELECT COUNT(*) FROM bill_payments").fetchone()[0] == 1
    conn.execute("UPDATE transactions SET excluded_reason = 'investment' WHERE id = 'tx-2'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET excluded_reason = 'bogus'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET review_flag = 'bogus'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET owner_person_id = 'nobody'")
    conn.close()


def test_people_store(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(config.db_path())
    salt = db.load_or_create_salt(config.data_dir())
    conn.executescript(
        "INSERT INTO profiles VALUES ('casa', 'Casa', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('item-1', 'casa'), ('item-2', 'casa');"
        "INSERT INTO accounts (id, item_id, kind, name, number_masked) VALUES "
        "('aaaaaaaa-1', 'item-1', 'checking', 'Conta', '73-6'),"
        "('aaaaaaaa-2', 'item-1', 'credit_card', 'Cartao', '0004'),"
        "('bbbbbbbb-3', 'item-2', 'checking', 'Outra', '1004');"
    )
    people = db.People(conn, salt)
    people.add("ana", "Ana")
    with pytest.raises(sqlite3.IntegrityError):
        people.add("ana", "Ana de novo")

    def acc_id(ref: str) -> str | None:
        row = people.find_account(ref)
        return None if row is None else str(row["id"])

    found = people.find("ANA")
    assert found is not None and found["id"] == "ana"
    assert people.find("nobody") is None
    assert acc_id("aaaaaaaa-1") == "aaaaaaaa-1"
    assert acc_id("73-6") == acc_id("736") == "aaaaaaaa-1"
    assert acc_id("bbbbbb") == "bbbbbbbb-3"
    assert acc_id("9999") is None
    with pytest.raises(db.AmbiguousAccountError):
        people.find_account("004")
    with pytest.raises(db.AmbiguousAccountError):
        people.find_account("aaaaaaaa")

    # identity doc learned before the owner is known; attached once all the
    # item's accounts belong to one person.
    people.set_item_identity("item-1", "1" * 11, "casa")
    assert people.has_item_identity("item-1") and not people.has_item_identity("item-2")
    row = conn.execute("SELECT person_id, source FROM household_docs").fetchone()
    assert tuple(row) == (None, "identity")
    people.assign("aaaaaaaa-1", "ana")
    assert conn.execute("SELECT person_id FROM household_docs").fetchone()[0] is None
    people.assign("aaaaaaaa-2", "ana")
    assert conn.execute("SELECT person_id FROM household_docs").fetchone()[0] == "ana"
    # A manual entry of the same document is not overridden by identity.
    assert people.add_doc("1" * 11, person_id="ana") is False
    assert people.add_doc("2" * 11, person_id="ana") is True
    people.set_item_identity("item-1", "1" * 11, "casa")
    sources = dict(conn.execute("SELECT doc_hash, source FROM household_docs").fetchall())
    assert set(sources.values()) == {"manual"}
    assert all(len(h) == 64 for h in sources)
    assert [tuple(r)[:4] for r in people.all()] == [("ana", "Ana", 2, 2)]
    conn.close()

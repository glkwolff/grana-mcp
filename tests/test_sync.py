from __future__ import annotations

import copy
from datetime import date

import pytest

from grana_mcp import config
from grana_mcp.config import Profile
from grana_mcp.store import db
from grana_mcp.sync import sync_profile
from tests.fakes import CARD_ID, ITEM_ID, FakePluggy, load

TODAY = date(2026, 11, 15)
PROFILE = Profile(id="ana", display_name="Ana", item_ids=[ITEM_ID])


@pytest.fixture
def store() -> db.Store:
    return db.Store(db.connect(config.db_path()), db.load_or_create_salt(config.data_dir()))


def _count(store: db.Store, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def test_sync_stores_everything(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r.items, r.accounts, r.fetched, r.new, r.updated, r.deleted) == (1, 2, 8, 8, 0, 0)
    assert r.sign_mismatches == 0
    item = store.conn.execute("SELECT * FROM items").fetchone()
    assert item["consent_expires_at"] == "2027-01-10T12:00:00+00:00"
    assert item["connector_name"] == "MeuPluggy"
    assert item["last_synced_at"] is not None
    amounts = dict(store.conn.execute("SELECT id, amount_cents FROM transactions").fetchall())
    assert sum(amounts.values()) == -15025 + 500000 - 123456 - 1010 - 8990 + 123456 - 5537 + 1999


def test_resync_is_idempotent(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
        n = _count(store, "transactions")
        r2 = sync_profile(store, c, PROFILE, today=TODAY)
    assert _count(store, "transactions") == n == 8
    assert (r2.new, r2.updated, r2.deleted) == (0, 0, 0)
    runs = store.conn.execute("SELECT status, n_new FROM sync_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("ok", 8), ("ok", 0)]


def test_resync_updates_in_place_and_keeps_pipeline_columns(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    tx_id = "44444444-4444-4444-8444-000000000001"
    store.conn.execute("UPDATE transactions SET category = 'Compras' WHERE id = ?", (tx_id,))
    store.conn.commit()
    fake.pages[CARD_ID][""]["results"][0]["status"] = "PENDING"
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r.new, r.updated) == (0, 1)
    row = store.conn.execute("SELECT status, category FROM transactions WHERE id = ?", (tx_id,))
    assert tuple(row.fetchone()) == ("PENDING", "Compras")


def test_recreated_card_transaction_replaces_old_id(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    # Pluggy deletes and re-creates card rows with a new id around bill closing.
    results = fake.pages[CARD_ID][""]["results"]
    clone = copy.deepcopy(results[0])
    clone["id"] = "44444444-4444-4444-8444-0000000000a1"
    results[0] = clone
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r.new, r.deleted) == (1, 1)
    assert _count(store, "transactions") == 8


def test_empty_read_never_wipes_local_data(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    fake.pages[CARD_ID][""] = {"results": [], "next": None}
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert r.deleted == 0
    assert _count(store, "transactions") == 8
    assert any("no transactions" in w for w in r.warnings)


def test_documents_are_stored_only_as_salted_hash(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    hashes = [
        r[0]
        for r in store.conn.execute(
            "SELECT counterpart_doc_hash FROM transactions WHERE counterpart_doc_hash IS NOT NULL"
        )
    ]
    salt = db.load_or_create_salt(config.data_dir())
    assert sorted(hashes) == sorted(
        [db.hash_doc("99999999999", salt), db.hash_doc("00000000000000", salt)]
    )
    store.conn.close()
    raw = config.db_path().read_bytes()
    for plain in (b"99999999999", b"999.999.999-99", b"00.000.000/0000-00"):
        assert plain not in raw


def test_failed_sync_is_recorded(store: db.Store) -> None:
    fake = FakePluggy()
    bad = Profile(id="ana", display_name="Ana", item_ids=["00000000-0000-4000-8000-00000000dead"])
    with fake.client() as c, pytest.raises(Exception, match="404"):
        sync_profile(store, c, bad, today=TODAY)
    run = store.conn.execute("SELECT status, error FROM sync_runs").fetchone()
    assert run["status"] == "error"
    assert "ITEM_NOT_FOUND" in run["error"]


def test_sync_runs_dedupe(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert r.dedupe is not None
    # Fixture: checking "PAGAMENTO FATURA" + card "Pagamento recebido", same amount/day.
    assert r.dedupe.by_reason["card_bill_payment"] == 2
    assert r.dedupe.flags["unmatched_refund"] == 1  # partial card refund
    rows = dict(
        store.conn.execute(
            "SELECT description_raw, excluded_reason FROM transactions "
            "WHERE excluded_reason IS NOT NULL"
        ).fetchall()
    )
    assert set(rows.values()) == {"card_bill_payment"}


def test_sync_learns_identity_as_hash_only(store: db.Store) -> None:
    fake = FakePluggy(identity=load("identity.json"))
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
        assert r.identities_learned == 1
        r2 = sync_profile(store, c, PROFILE, today=TODAY)
    assert r2.identities_learned == 0
    assert sum(1 for x in fake.requests if x.url.path == "/identity") == 1  # once per item
    salt = db.load_or_create_salt(config.data_dir())
    row = store.conn.execute("SELECT doc_hash, source, profile_id FROM household_docs").fetchone()
    assert tuple(row) == (db.hash_doc("0" * 11, salt), "identity", "ana")
    assert store.conn.execute("SELECT item_id FROM item_identities").fetchone()[0] == ITEM_ID
    store.conn.close()
    raw = config.db_path().read_bytes()
    for plain in (b"Titular", b"Rua Sintetica", b"example.invalid", b"Conjuge", b"1990-01-01"):
        assert plain not in raw


def test_sync_identity_optional(store: db.Store) -> None:
    fake = FakePluggy(identity=load("identity.json"), identity_status=403)
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert any("identity unavailable (403)" in w for w in r.warnings)
    fake = FakePluggy(identity=load("identity.json"))
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY, identity=False)
    assert r.identities_learned == 0
    assert not any(x.url.path == "/identity" for x in fake.requests)
    fake = FakePluggy(identity=load("identity.json"), identity_status=401)
    with fake.client() as c, pytest.raises(Exception, match="401"):
        sync_profile(store, c, PROFILE, today=TODAY)

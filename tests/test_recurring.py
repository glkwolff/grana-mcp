from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from grana_mcp import config
from grana_mcp.pipeline import recurring as rec
from grana_mcp.store import db

TODAY = date(2026, 11, 15)


def charge(key: str, posted: date, cents: int, category: str = "Assinaturas") -> rec.Charge:
    return rec.Charge(key=key, label=key.upper(), category=category, posted=posted, cents=cents)


def monthly(key: str, start: date, amounts: list[int]) -> list[rec.Charge]:
    return [charge(key, start + timedelta(days=30 * i), a) for i, a in enumerate(amounts)]


def test_merchant_key_drops_reference_codes() -> None:
    assert rec.merchant_key("Spotify P46FB") == "spotify"
    assert rec.merchant_key("Raia2153") == ""
    assert rec.merchant_key("99Food Lanchonete") == "99food lanchonete"


def test_detects_monthly_subscription_with_price_change() -> None:
    charges = monthly("streaming", date(2026, 6, 10), [2190, 2190, 2190, 2390, 2390])
    [s] = rec.detect(charges, TODAY)
    assert s.occurrences == 5
    assert s.active
    assert s.last_cents == 2390
    assert s.previous_cents == 2390
    assert not s.price_changed
    assert s.next_expected == s.last_date + timedelta(days=30)


def test_price_change_flag() -> None:
    charges = monthly("streaming", date(2026, 8, 10), [2190, 2190, 2390])
    [s] = rec.detect(charges, TODAY)
    assert s.price_changed


def test_needs_three_occurrences_and_monthly_gaps() -> None:
    assert rec.detect(monthly("x", date(2026, 9, 1), [1000, 1000]), TODAY) == []
    weekly = [charge("y", date(2026, 9, 1) + timedelta(days=7 * i), 1000) for i in range(8)]
    assert rec.detect(weekly, TODAY) == []
    gappy = [charge("z", date(2026, 1, 1), 1000), charge("z", date(2026, 4, 1), 1000),
             charge("z", date(2026, 7, 1), 1000)]  # fmt: skip
    assert rec.detect(gappy, TODAY) == []


def test_amount_outside_tolerance_breaks_series() -> None:
    charges = monthly("x", date(2026, 8, 1), [1000, 5000, 1000])
    assert rec.detect(charges, TODAY) == []


def test_frequent_merchant_is_not_a_subscription() -> None:
    # A supermarket visited every few days forms a monthly chain by chance.
    base = date(2026, 6, 1)
    charges = [charge("mercado", base + timedelta(days=3 * i), 5000 + (i % 3) * 100, "Mercado")
               for i in range(50)]  # fmt: skip
    assert rec.detect(charges, TODAY) == []


def test_inactive_when_last_charge_is_old() -> None:
    [s] = rec.detect(monthly("old", date(2026, 3, 1), [1000, 1000, 1000]), TODAY)
    assert not s.active


def test_empty_keys_are_ignored() -> None:
    assert rec.detect(monthly("", date(2026, 8, 1), [1000, 1000, 1000]), TODAY) == []


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO profiles VALUES ('ana', 'Ana', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('item-a', 'ana');"
        "INSERT INTO accounts (id, item_id, kind, name) VALUES ('chk', 'item-a', 'checking', 'C');"
    )
    yield c
    c.close()


def add(c: sqlite3.Connection, i: int, posted: date, desc: str, **kw: object) -> None:
    c.execute(
        "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
        "description_raw, description_clean, category, installment_total, counterpart_doc_type, "
        "operation_type, source, synced_at) "
        "VALUES (?, 'chk', 'ana', ?, -2390, ?, ?, 'Assinaturas', ?, ?, ?, 'pluggy', 'x')",
        (f"t{i}", posted.isoformat(), desc, desc, kw.get("inst"), kw.get("doc"), kw.get("op")),
    )


def test_load_charges_skips_installments_and_person_transfers(conn: sqlite3.Connection) -> None:
    for i in range(3):
        d = date(2026, 8, 10) + timedelta(days=30 * i)
        add(conn, i, d, "STREAMING SINTETICO")
        add(conn, 10 + i, d, "LOJA PARCELADA", inst=3)
        add(conn, 20 + i, d, "Pix enviado FULANO", doc="CPF", op="PIX")
    conn.commit()
    charges = rec.load_charges(conn, TODAY)
    assert {c.key for c in charges} == {"streaming sintetico"}
    [s] = rec.detect(charges, TODAY)
    assert s.label == "STREAMING SINTETICO"

"""Closed credit card bills: client normalization, pagination and sync idempotency."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import date

import pytest

from grana_mcp import config
from grana_mcp.config import Profile
from grana_mcp.providers.base import Account
from grana_mcp.providers.pluggy import calendar_date
from grana_mcp.store import db
from grana_mcp.sync import sync_profile
from tests.fakes import CARD_ID, CHECKING_ID, ITEM_ID, FakePluggy, load

TODAY = date(2026, 11, 15)
PROFILE = Profile(id="ana", display_name="Ana", item_ids=[ITEM_ID])
CARD = Account(CARD_ID, ITEM_ID, "credit_card", "Cartao", "****0000", "BRL")


@pytest.fixture
def store() -> Iterator[db.Store]:
    s = db.Store(db.connect(config.db_path()), db.load_or_create_salt(config.data_dir()))
    yield s
    s.conn.close()


def test_calendar_date_keeps_the_date() -> None:
    assert calendar_date("2026-11-01T00:00:00.000Z") == date(2026, 11, 1)
    assert calendar_date("2023-07-20T00:00:00") == date(2023, 7, 20)


def test_list_bills_normalizes() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        bills = c.list_bills(CARD)
    assert [b.id[-1] for b in bills] == ["2", "1"]
    b = bills[0]
    assert b.due_date == date(2026, 10, 10)
    assert b.closing_date == date(2026, 10, 3)  # UTC midnight kept as-is
    assert b.total_cents == 123457  # 1234.565 -> half-up
    assert b.minimum_payment_cents == 18518
    assert b.allows_installments is True
    assert len(b.payments) == 1
    assert b.payments[0].amount_cents == 123457
    assert b.payments[0].payment_date == date(2026, 10, 10)
    assert b.payments[0].payment_mode == "DEBIT_ACCOUNT"
    old = bills[1]
    assert (old.closing_date, old.total_cents, old.minimum_payment_cents) == (None, 9999, None)
    req = next(r for r in fake.requests if r.url.path == "/bills")
    assert req.method == "GET"
    assert req.url.params["accountId"] == CARD_ID
    assert req.url.params["page"] == "1"


def test_list_bills_follows_pages() -> None:
    fake = FakePluggy()
    page1 = load("bills_card.json")
    page2 = copy.deepcopy(page1)
    page1["results"] = page1["results"][:1]
    page1["totalPages"] = page2["totalPages"] = 2
    page2["page"] = 2
    page2["results"] = page2["results"][1:]
    fake.bills[CARD_ID] = [page1, page2]
    with fake.client() as c:
        bills = c.list_bills(CARD)
    assert len(bills) == 2
    pages = [r.url.params["page"] for r in fake.requests if r.url.path == "/bills"]
    assert pages == ["1", "2"]


def _counts(store: db.Store) -> tuple[int, int]:
    b = store.conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    p = store.conn.execute("SELECT COUNT(*) FROM bill_payments").fetchone()[0]
    return int(b), int(p)


def test_sync_fetches_bills_only_for_cards_and_is_idempotent(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        r1 = sync_profile(store, c, PROFILE, today=TODAY)
        r2 = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r1.bills_new, r1.bills_updated) == (2, 0)
    assert (r2.bills_new, r2.bills_updated) == (0, 0)
    assert _counts(store) == (2, 1)
    bill_accounts = {r.url.params["accountId"] for r in fake.requests if r.url.path == "/bills"}
    assert bill_accounts == {CARD_ID}
    assert CHECKING_ID not in bill_accounts
    row = store.conn.execute(
        "SELECT due_date, closing_date, total_cents, allows_installments FROM bills "
        "WHERE closing_date IS NOT NULL"
    ).fetchone()
    assert tuple(row) == ("2026-10-10", "2026-10-03", 123457, 1)


def test_sync_updates_changed_bill_and_replaces_payments(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    older = fake.bills[CARD_ID][0]["results"][1]
    older["payments"] = [
        {"id": "p1", "valueType": "OTHER_PAYMENT", "paymentDate": "2026-09-09T00:00:00.000Z",
         "paymentMode": "PIX", "amount": 50, "currencyCode": "BRL"},
        {"id": "p2", "valueType": "OTHER_PAYMENT", "paymentDate": "2026-09-10T00:00:00.000Z",
         "paymentMode": "PIX", "amount": 49.99, "currencyCode": "BRL"},
    ]  # fmt: skip
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r.bills_new, r.bills_updated) == (0, 1)
    assert _counts(store) == (2, 3)
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert (r.bills_new, r.bills_updated) == (0, 0)
    assert _counts(store) == (2, 3)


def test_bills_unavailable_is_a_warning_not_a_failure(store: db.Store) -> None:
    fake = FakePluggy(bills_status=404)
    with fake.client() as c:
        r = sync_profile(store, c, PROFILE, today=TODAY)
    assert r.new == 8
    assert any("bills unavailable" in w for w in r.warnings)
    assert _counts(store) == (0, 0)


def test_bills_server_error_fails_the_sync(store: db.Store) -> None:
    fake = FakePluggy(bills_status=500)
    with fake.client() as c, pytest.raises(Exception, match="500"):
        sync_profile(store, c, PROFILE, today=TODAY)


def test_counterpart_doc_type_is_stored(store: db.Store) -> None:
    fake = FakePluggy()
    with fake.client() as c:
        sync_profile(store, c, PROFILE, today=TODAY)
    types = {
        r[0]
        for r in store.conn.execute(
            "SELECT counterpart_doc_type FROM transactions WHERE counterpart_doc_hash IS NOT NULL"
        )
    }
    assert types == {"CPF", "CNPJ"}

"""U4 (sign convention), dates, documents and account mapping."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from grana_mcp.providers.base import AccountKind
from grana_mcp.providers.pluggy import (
    account_kind,
    document_digits,
    mask_number,
    posted_date,
    signed_cents,
)


@pytest.mark.parametrize(
    ("amount", "type_", "kind", "expected"),
    [
        # Checking account: amount is negative for outflows (API examples).
        ("-150.25", "DEBIT", "checking", -15025),
        ("5000.00", "CREDIT", "checking", 500000),
        # Credit card: amount is positive for purchases (docs: "Sign Convention
        # for Credit Card Transactions"), negative for payments/refunds.
        ("89.90", "DEBIT", "credit_card", -8990),
        ("-1234.56", "CREDIT", "credit_card", 123456),
        ("-19.99", "CREDIT", "credit_card", 1999),
    ],
)
def test_sign_convention_spending_is_negative(
    amount: str, type_: str, kind: AccountKind, expected: int
) -> None:
    cents, mismatch = signed_cents(Decimal(amount), None, type_, kind)
    assert cents == expected
    assert mismatch is False


@pytest.mark.parametrize(
    ("amount", "kind", "expected"),
    [
        ("-150.25", "checking", -15025),
        ("150.25", "checking", 15025),
        ("89.90", "credit_card", -8990),
        ("-1234.56", "credit_card", 123456),
        ("-10.00", "other", -1000),
    ],
)
def test_sign_fallback_without_type(amount: str, kind: AccountKind, expected: int) -> None:
    assert signed_cents(Decimal(amount), None, None, kind) == (expected, False)


def test_sign_mismatch_is_flagged_and_type_wins() -> None:
    # A card row whose amount sign contradicts its type: trust `type`, flag it.
    cents, mismatch = signed_cents(Decimal("-50.00"), None, "DEBIT", "credit_card")
    assert cents == -5000
    assert mismatch is True


def test_amount_in_account_currency_is_the_magnitude() -> None:
    cents, _ = signed_cents(Decimal("10.00"), Decimal("55.37"), "DEBIT", "credit_card")
    assert cents == -5537


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-05T14:22:10.123Z", date(2026, 10, 5)),
        # Late evening in São Paulo is the next day in UTC.
        ("2026-10-01T01:30:00.000Z", date(2026, 9, 30)),
        # 03:00Z is São Paulo midnight.
        ("2026-10-01T03:00:00.000Z", date(2026, 10, 1)),
        # Exactly UTC midnight is a calendar date: keep it.
        ("2026-10-01T00:00:00.000Z", date(2026, 10, 1)),
        ("2026-10-01", date(2026, 10, 1)),
    ],
)
def test_posted_date_sao_paulo(raw: str, expected: date) -> None:
    assert posted_date(raw) == expected


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ("000.000.000-00", "00000000000"),
        ("00.000.000/0000-00", "00000000000000"),
        ("***.***.000-00", None),  # masked
        ("123", None),
        (None, None),
    ],
)
def test_document_digits(doc: str | None, expected: str | None) -> None:
    assert document_digits(doc) == expected


def test_account_kind_and_mask() -> None:
    assert account_kind("CHECKING_ACCOUNT", "BANK") == "checking"
    assert account_kind("CREDIT_CARD", "CREDIT") == "credit_card"
    assert account_kind("SAVINGS_ACCOUNT", "BANK") == "other"
    assert mask_number("0000/12345-6") == "****45-6"
    assert mask_number(None) is None

"""Open bill estimate. Synthetic numbers only."""

from __future__ import annotations

from datetime import date

from grana_mcp.analytics.card_bill import (
    CardTx,
    ClosedBill,
    estimate_open_bill,
    last_closed_bill,
    scheduled_by_month,
)

TODAY = date(2026, 11, 15)
CLOSING = date(2026, 11, 3)
PREV = ClosedBill("b2", CLOSING, date(2026, 11, 10), 100_00)
OLDER = ClosedBill("b1", date(2026, 10, 3), date(2026, 10, 10), 77_77)


def tx(i: str, d: date, cents: int, op: str | None = None, cat: str | None = None) -> CardTx:
    return CardTx(i, d, cents, op, cat)


def test_estimate_basic() -> None:
    txs = [
        tx("old", date(2026, 11, 2), -999_00),  # previous bill
        tx("closing-day", CLOSING, -10_00),  # installment dated on the closing day: new bill
        tx("p1", date(2026, 11, 5), -20_00),
        tx("p2", TODAY, -5_50),
        tx("settle", date(2026, 11, 10), 100_01, op="PAGAMENTO_FATURA"),  # ±1 cent
        tx("early1", date(2026, 11, 12), 7_00, op="PAGAMENTO_FATURA"),
        tx("early2", date(2026, 11, 13), 3_00, cat="Credit card payment"),
        tx("refund", date(2026, 11, 14), 2_00),
        tx("future", date(2026, 12, 3), -10_00),
        tx("future2", date(2027, 1, 3), -10_00),
        tx("future3", date(2026, 12, 20), -1_00),
        tx("future-credit", date(2026, 12, 1), 1_00),
    ]
    est = estimate_open_bill(txs, [OLDER, PREV], TODAY)
    assert est.closing_date == CLOSING
    assert est.previous_bill_id == "b2"
    assert est.previous_bill_payment_id == "settle"
    assert est.previous_bill_payment_date == date(2026, 11, 10)
    assert (est.purchases_cents, est.n_purchases) == (35_50, 3)
    assert (est.early_payments_cents, est.n_early_payments) == (10_00, 2)
    assert (est.other_credits_cents, est.n_other_credits) == (2_00, 1)
    assert est.estimated_cents == 35_50 - 10_00 - 2_00
    assert est.scheduled_by_month == {"2026-12": (11_00, 2), "2027-01": (10_00, 1)}
    assert any("NuPay" in c for c in est.caveats)
    assert not any("não foi encontrado" in c for c in est.caveats)


def test_only_one_settlement_payment_is_excluded() -> None:
    txs = [
        tx("a", date(2026, 11, 4), 100_00, op="PAGAMENTO_FATURA"),
        tx("b", date(2026, 11, 6), 100_00, op="PAGAMENTO_FATURA"),
        tx("p", date(2026, 11, 7), -300_00),
    ]
    est = estimate_open_bill(txs, [PREV], TODAY)
    assert est.previous_bill_payment_id == "a"
    assert est.early_payments_cents == 100_00
    assert est.estimated_cents == 200_00


def test_tolerance_is_one_cent() -> None:
    txs = [tx("x", date(2026, 11, 4), 100_02, op="PAGAMENTO_FATURA")]
    est = estimate_open_bill(txs, [PREV], TODAY)
    assert est.previous_bill_payment_id is None
    assert est.early_payments_cents == 100_02
    assert any("não foi encontrado" in c for c in est.caveats)


def test_credit_on_closing_day_is_not_counted() -> None:
    txs = [tx("c", CLOSING, 5_00, op="PAGAMENTO_FATURA"), tx("p", CLOSING, -5_00)]
    est = estimate_open_bill(txs, [PREV], TODAY)
    assert est.early_payments_cents == 0
    assert est.purchases_cents == 5_00


def test_no_bill_gives_no_estimate() -> None:
    est = estimate_open_bill([tx("p", TODAY, -1_00)], [], TODAY)
    assert est.estimated_cents is None
    assert est.closing_date is None
    assert "não dá para estimar" in est.caveats[0]


def test_bill_without_closing_date_is_ignored() -> None:
    no_closing = ClosedBill("b", None, date(2026, 11, 10), 1_00)
    assert last_closed_bill([no_closing], TODAY) is None
    assert estimate_open_bill([], [no_closing], TODAY).estimated_cents is None


def test_last_closed_bill_ignores_future_closing() -> None:
    future = ClosedBill("f", date(2026, 12, 3), date(2026, 12, 10), 1_00)
    assert last_closed_bill([OLDER, future, PREV], TODAY) == PREV


def test_scheduled_ignores_credits_and_past() -> None:
    txs = [tx("a", TODAY, -1_00), tx("b", date(2026, 12, 1), 1_00)]
    assert scheduled_by_month(txs, TODAY) == {}

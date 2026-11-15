"""Open credit card bill estimate (pure functions, no I/O).

Pluggy's ``GET /bills`` only returns closed bills. The open bill is estimated
from the card's transactions:

* purchases: debits (``amount_cents < 0``) with
  ``last_closing <= posted_date <= today``. Inclusive on the closing day:
  installments dated on the closing day belong to the new bill;
* credits: credits (``amount_cents > 0``) with ``last_closing < posted_date <= today``,
  EXCEPT the one that settled the previous bill (amount equal to the previous
  bill's total, ±1 cent). The rest are early payments ("antecipações") or
  refunds/other credits;
* estimate = purchases - early payments - other credits;
* scheduled: debits dated after ``today`` (future installments), per month.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

SETTLEMENT_TOLERANCE_CENTS = 1
PAYMENT_OPERATION = "PAGAMENTO_FATURA"
PAYMENT_CATEGORY = "credit card payment"

CAVEATS_BASE = (
    "Estimativa: o valor oficial só existe quando a fatura fecha.",
    "Compras via NuPay (Nubank) ou outras linhas de crédito separadas (ex.: 'Limite "
    "Nupay') podem não aparecer como transações do Open Finance.",
    "Compras recentes podem ainda não ter chegado (o Meu Pluggy atualiza a cada 24 h).",
)


@dataclass(frozen=True, slots=True)
class CardTx:
    id: str
    posted_date: date
    amount_cents: int  # internal convention: purchase < 0, payment/refund > 0
    operation_type: str | None = None
    provider_category: str | None = None

    @property
    def is_payment(self) -> bool:
        return (self.operation_type or "").upper() == PAYMENT_OPERATION or (
            self.provider_category or ""
        ).strip().lower() == PAYMENT_CATEGORY


@dataclass(frozen=True, slots=True)
class ClosedBill:
    id: str
    closing_date: date | None
    due_date: date
    total_cents: int


@dataclass
class OpenBillEstimate:
    closing_date: date | None
    previous_bill_id: str | None
    previous_bill_total_cents: int | None
    previous_bill_payment_id: str | None
    previous_bill_payment_date: date | None
    purchases_cents: int = 0
    n_purchases: int = 0
    early_payments_cents: int = 0
    n_early_payments: int = 0
    other_credits_cents: int = 0
    n_other_credits: int = 0
    estimated_cents: int | None = None
    scheduled_by_month: dict[str, tuple[int, int]] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


def last_closed_bill(bills: Iterable[ClosedBill], today: date) -> ClosedBill | None:
    """Most recent bill with a closing date on or before ``today``."""
    closed = [b for b in bills if b.closing_date is not None and b.closing_date <= today]
    return max(closed, key=lambda b: (b.closing_date or date.min, b.due_date), default=None)


def scheduled_by_month(txs: Iterable[CardTx], today: date) -> dict[str, tuple[int, int]]:
    """Debits dated after ``today``: ``{"YYYY-MM": (cents, count)}``, cents positive."""
    out: dict[str, tuple[int, int]] = {}
    for t in txs:
        if t.posted_date > today and t.amount_cents < 0:
            key = t.posted_date.strftime("%Y-%m")
            cents, n = out.get(key, (0, 0))
            out[key] = (cents - t.amount_cents, n + 1)
    return dict(sorted(out.items()))


def estimate_open_bill(
    txs: Iterable[CardTx], bills: Iterable[ClosedBill], today: date
) -> OpenBillEstimate:
    txs = list(txs)
    previous = last_closed_bill(bills, today)
    est = OpenBillEstimate(
        closing_date=previous.closing_date if previous else None,
        previous_bill_id=previous.id if previous else None,
        previous_bill_total_cents=previous.total_cents if previous else None,
        previous_bill_payment_id=None,
        previous_bill_payment_date=None,
        scheduled_by_month=scheduled_by_month(txs, today),
        caveats=list(CAVEATS_BASE),
    )
    if previous is None or previous.closing_date is None:
        est.caveats.insert(
            0,
            "Nenhuma fatura fechada com data de fechamento: não dá para estimar a fatura aberta.",
        )
        return est
    closing = previous.closing_date

    credits = sorted(
        (t for t in txs if t.amount_cents > 0 and closing < t.posted_date <= today),
        key=lambda t: (t.posted_date, t.id),
    )
    settlement = next(
        (
            t
            for t in credits
            if abs(t.amount_cents - previous.total_cents) <= SETTLEMENT_TOLERANCE_CENTS
        ),
        None,
    )
    if settlement is not None:
        est.previous_bill_payment_id = settlement.id
        est.previous_bill_payment_date = settlement.posted_date
    elif previous.total_cents > 0:
        est.caveats.insert(
            0,
            "O pagamento da fatura anterior não foi encontrado; todos os créditos após o "
            "fechamento foram descontados (a estimativa pode estar baixa).",
        )

    for t in txs:
        if t.amount_cents < 0 and closing <= t.posted_date <= today:
            est.purchases_cents -= t.amount_cents
            est.n_purchases += 1
    for t in credits:
        if settlement is not None and t.id == settlement.id:
            continue
        if t.is_payment:
            est.early_payments_cents += t.amount_cents
            est.n_early_payments += 1
        else:
            est.other_credits_cents += t.amount_cents
            est.n_other_credits += 1
    est.estimated_cents = est.purchases_cents - est.early_payments_cents - est.other_credits_cents
    return est

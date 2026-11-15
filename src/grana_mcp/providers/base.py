"""Provider-agnostic models and the ``Provider`` protocol.

Every provider returns data already normalized to the internal conventions:

* money is ``int`` cents;
* ``amount_cents`` is negative for money leaving the account / spending on a card,
  positive for money coming in (salary, refunds, card bill payment received);
* ``posted_date`` is a calendar date in ``America/Sao_Paulo``.

Providers are read-only by contract: there is no method that changes anything
at the data source.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

AccountKind = Literal["checking", "credit_card", "other"]


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    connector_name: str | None
    status: str | None
    consent_expires_at: datetime | None
    last_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    item_id: str
    kind: AccountKind
    name: str
    number_masked: str | None
    currency: str


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    account_id: str
    posted_date: date
    amount_cents: int
    status: str | None
    description_raw: str
    merchant: str | None
    provider_category: str | None
    operation_type: str | None
    installment_number: int | None
    installment_total: int | None
    # Raw CPF/CNPJ digits of the counterpart, kept in memory only. The store
    # hashes it (salted SHA-256) before anything is written to disk.
    counterpart_doc: str | None
    # True when the provider's own direction flag disagreed with the sign rule.
    sign_mismatch: bool = False


@dataclass(frozen=True, slots=True)
class BillPayment:
    """A payment recorded on a closed bill. ``amount_cents`` is positive."""

    payment_date: date | None
    amount_cents: int
    value_type: str | None
    payment_mode: str | None


@dataclass(frozen=True, slots=True)
class Bill:
    """A closed credit card bill (fatura). Amounts are positive cents.

    Dates are calendar dates as sent by the provider (no timezone conversion).
    """

    id: str
    account_id: str
    due_date: date
    closing_date: date | None
    total_cents: int
    currency: str
    minimum_payment_cents: int | None
    allows_installments: bool | None
    payments: tuple[BillPayment, ...] = ()


class Provider(Protocol):
    """Read-only access to one data source."""

    def get_item(self, item_id: str) -> Item: ...

    def list_items(self, item_ids: list[str]) -> list[Item]: ...

    def list_accounts(self, item_id: str) -> list[Account]: ...

    def list_transactions(
        self, account: Account, date_from: date, date_to: date | None = None
    ) -> Iterator[Transaction]: ...

    def list_bills(self, account: Account) -> list[Bill]: ...

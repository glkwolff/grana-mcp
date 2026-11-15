"""Read-only Pluggy API client.

Only these HTTP calls exist in this module, on purpose:

* ``POST /auth``              exchange client id/secret for an API key (2 h)
* ``GET  /items/{id}``        item status and ``consentExpiresAt``
* ``GET  /accounts?itemId=``  accounts of an item
* ``GET  /v2/transactions``   transactions of an account, cursor pagination
* ``GET  /bills?accountId=``  closed credit card bills of an account, page pagination

There is no code for payments, Pix, transfers, connect tokens, item
update/refresh/delete or anything else that writes. See CLAUDE.md, rule 1.
Field-by-field sources are in ``docs/pluggy-mapping.md``.

Logging: never log secrets, the API key, amounts, descriptions or documents.
Only counts and ids.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict

from grana_mcp import __version__
from grana_mcp.money import to_cents
from grana_mcp.providers.base import (
    Account,
    AccountKind,
    Bill,
    BillPayment,
    Item,
    Transaction,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.pluggy.ai"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

# API keys live 2 hours (docs/reference/authentication). Renew a bit earlier.
API_KEY_TTL_SECONDS = 2 * 60 * 60
API_KEY_RENEW_MARGIN_SECONDS = 10 * 60
MAX_RATE_LIMIT_RETRIES = 3
TRANSACTIONS_PATH = "/v2/transactions"
BILLS_PAGE_SIZE = 500  # documented maximum (OpenAPI, GET /bills)
MAX_BILL_PAGES = 20


class PluggyError(Exception):
    """An error response from the Pluggy API (no secrets in the message)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class PluggyAuthError(PluggyError):
    """``POST /auth`` rejected the client id/secret."""


# --- Raw API payloads (only the fields we read; unknown fields are ignored) ---


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RawConnector(_Raw):
    id: int | None = None
    name: str | None = None


class _RawItem(_Raw):
    id: str
    status: str | None = None
    executionStatus: str | None = None
    connector: _RawConnector | None = None
    consentExpiresAt: datetime | None = None
    lastUpdatedAt: datetime | None = None


class _RawAccount(_Raw):
    id: str
    itemId: str
    type: str | None = None
    subtype: str | None = None
    name: str | None = None
    number: str | None = None
    currencyCode: str | None = None


class _RawDocument(_Raw):
    type: str | None = None
    value: str | None = None


class _RawParticipant(_Raw):
    # OpenAPI says object {type, value}; accept a plain string too, defensively.
    documentNumber: _RawDocument | str | None = None


class _RawPaymentData(_Raw):
    payer: _RawParticipant | None = None
    receiver: _RawParticipant | None = None


class _RawCreditCardMetadata(_Raw):
    installmentNumber: int | None = None
    totalInstallments: int | None = None


class _RawMerchant(_Raw):
    name: str | None = None


class _RawTransaction(_Raw):
    id: str
    accountId: str | None = None
    date: str
    description: str | None = None
    descriptionRaw: str | None = None
    amount: Decimal
    amountInAccountCurrency: Decimal | None = None
    type: Literal["DEBIT", "CREDIT"] | None = None
    status: str | None = None
    category: str | None = None
    operationType: str | None = None
    paymentData: _RawPaymentData | None = None
    creditCardMetadata: _RawCreditCardMetadata | None = None
    merchant: _RawMerchant | None = None


class _RawBillPayment(_Raw):
    paymentDate: str | None = None
    amount: Decimal
    valueType: str | None = None
    paymentMode: str | None = None


class _RawBill(_Raw):
    id: str
    dueDate: str
    billClosingDate: str | None = None
    totalAmount: Decimal
    totalAmountCurrencyCode: str | None = None
    minimumPaymentAmount: Decimal | None = None
    allowsInstallments: bool | None = None
    payments: list[_RawBillPayment] | None = None


# --- Normalization (pure functions, unit-tested) ---


def account_kind(subtype: str | None, type_: str | None) -> AccountKind:
    """Map Pluggy ``subtype``/``type`` to the internal account kind."""
    if subtype == "CHECKING_ACCOUNT":
        return "checking"
    if subtype == "CREDIT_CARD" or type_ == "CREDIT":
        return "credit_card"
    return "other"  # e.g. SAVINGS_ACCOUNT


def mask_number(number: str | None) -> str | None:
    """Keep only the last 4 characters of an account/card number."""
    if not number:
        return None
    tail = number[-4:]
    return f"****{tail}"


def signed_cents(
    amount: Decimal,
    amount_in_account_currency: Decimal | None,
    type_: str | None,
    kind: AccountKind,
) -> tuple[int, bool]:
    """Return ``(amount_cents, sign_mismatch)`` in the internal convention.

    Internal convention: money out / spending is negative.

    * Magnitude: ``amountInAccountCurrency`` when present (international
      transactions), else ``amount``.
    * Sign: from ``type`` (``DEBIT`` = out = negative, ``CREDIT`` = in = positive).
      The OpenAPI spec documents ``type`` as the holder-perspective direction and
      says Pluggy normalizes it for credit cards.
    * Fallback when ``type`` is missing: the documented ``amount`` sign rule.
      Credit cards: positive ``amount`` = expense, so invert. Bank accounts:
      negative ``amount`` = outflow, so keep (only shown in API examples, not
      stated normatively; confirmed by brunopedrazza/pluggy-mcp-server on real data).

    ``sign_mismatch`` is True when ``type`` and the ``amount`` sign rule disagree;
    the sync reports how many, so U4 can be checked against real data.
    """
    magnitude = abs(
        amount_in_account_currency if amount_in_account_currency is not None else amount
    )
    cents = to_cents(magnitude)
    by_amount = -amount if kind == "credit_card" else amount
    if type_ == "DEBIT":
        result = -cents
    elif type_ == "CREDIT":
        result = cents
    else:
        result = cents if by_amount >= 0 else -cents
    mismatch = type_ is not None and amount != 0 and ((by_amount < 0) != (type_ == "DEBIT"))
    return result, mismatch


def posted_date(value: str) -> date:
    """Calendar date of a transaction in São Paulo time.

    Docs: ``date`` is ISO 8601 in UTC; convert to GMT-3 to read it as Brazilian
    time. Values pinned exactly to UTC midnight are calendar dates (converting
    them would move them to the previous day), so they are kept as-is. A bare
    ``YYYY-MM-DD`` is taken literally.
    """
    if len(value) == 10:
        return date.fromisoformat(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    if (utc.hour, utc.minute, utc.second, utc.microsecond) == (0, 0, 0, 0):
        return utc.date()
    return utc.astimezone(SAO_PAULO).date()


def calendar_date(value: str) -> date:
    """Date part of a bill date, kept as-is (no timezone conversion).

    Bill dates are calendar dates pinned to midnight (``2026-11-01T00:00:00.000Z``
    observed; ``2023-07-20T00:00:00`` in the OpenAPI examples), so the first 10
    characters are the date.
    """
    return date.fromisoformat(value[:10])


def normalize_bill(raw: _RawBill, account_id: str) -> Bill:
    payments = tuple(
        BillPayment(
            payment_date=calendar_date(p.paymentDate) if p.paymentDate else None,
            amount_cents=to_cents(abs(p.amount)),
            value_type=p.valueType,
            payment_mode=p.paymentMode,
        )
        for p in raw.payments or []
    )
    return Bill(
        id=raw.id,
        account_id=account_id,
        due_date=calendar_date(raw.dueDate),
        closing_date=calendar_date(raw.billClosingDate) if raw.billClosingDate else None,
        total_cents=to_cents(raw.totalAmount),
        currency=raw.totalAmountCurrencyCode or "BRL",
        minimum_payment_cents=(
            to_cents(raw.minimumPaymentAmount) if raw.minimumPaymentAmount is not None else None
        ),
        allows_installments=raw.allowsInstallments,
        payments=payments,
    )


def document_digits(doc: _RawDocument | str | None) -> str | None:
    """Digits of a CPF (11) or CNPJ (14). Masked or malformed values give None."""
    if doc is None:
        return None
    value = doc if isinstance(doc, str) else doc.value
    if not value or "*" in value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if len(digits) in (11, 14) else None


def normalize_transaction(raw: _RawTransaction, account: Account) -> Transaction:
    cents, mismatch = signed_cents(raw.amount, raw.amountInAccountCurrency, raw.type, account.kind)
    # UNVERIFIED for the MeuPluggy connector (listed as "Direct"): whether it
    # fills operationType / paymentData, which the docs describe as Open Finance
    # fields. See docs/pluggy-mapping.md (U3).
    counterpart = None
    if raw.paymentData is not None:
        # Money out: the other side is the receiver. Money in: the payer.
        party = raw.paymentData.receiver if cents < 0 else raw.paymentData.payer
        counterpart = document_digits(party.documentNumber) if party else None
    meta = raw.creditCardMetadata
    return Transaction(
        id=raw.id,
        account_id=account.id,
        posted_date=posted_date(raw.date),
        amount_cents=cents,
        status=raw.status,
        description_raw=raw.descriptionRaw or raw.description or "",
        merchant=raw.merchant.name if raw.merchant else None,
        provider_category=raw.category,
        operation_type=raw.operationType,
        installment_number=meta.installmentNumber if meta else None,
        installment_total=meta.totalInstallments if meta else None,
        counterpart_doc=counterpart,
        sign_mismatch=mismatch,
    )


def _decode(resp: httpx.Response) -> Any:
    # parse_float=Decimal: money never passes through float.
    return json.loads(resp.content, parse_float=Decimal)


def _error_from(resp: httpx.Response) -> PluggyError:
    code: str | None = None
    try:
        body = _decode(resp)
        if isinstance(body, dict):
            code = body.get("codeDescription")
    except ValueError:
        pass
    path = resp.request.url.path
    return PluggyError(
        f"Pluggy API error {resp.status_code} ({code or 'no code'}) on {path}",
        status=resp.status_code,
        code=code,
    )


class PluggyClient:
    """Synchronous, read-only Pluggy client. Implements ``providers.base.Provider``."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": f"grana-mcp/{__version__}"},
        )
        self._clock = clock
        self._sleep = sleep
        self._api_key: str | None = None
        self._api_key_at = 0.0

    def __repr__(self) -> str:  # never show credentials
        return "PluggyClient(<redacted>)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- auth ---

    def authenticate(self) -> None:
        """``POST /auth``. Raises ``PluggyAuthError`` on bad credentials."""
        resp = self._send(
            "POST",
            "/auth",
            json={"clientId": self._client_id, "clientSecret": self._client_secret},
        )
        if resp.status_code in (401, 403):
            err = _error_from(resp)
            raise PluggyAuthError(
                "Pluggy rejected the client id/secret", status=err.status, code=err.code
            )
        if resp.status_code != 200:
            raise _error_from(resp)
        body = _decode(resp)
        api_key = body.get("apiKey") if isinstance(body, dict) else None
        if not isinstance(api_key, str) or not api_key:
            raise PluggyError("Pluggy /auth response has no apiKey", status=resp.status_code)
        self._api_key = api_key
        self._api_key_at = self._clock()
        log.debug("authenticated with Pluggy")

    def _key(self) -> str:
        age = self._clock() - self._api_key_at
        if self._api_key is None or age >= API_KEY_TTL_SECONDS - API_KEY_RENEW_MARGIN_SECONDS:
            self.authenticate()
        if self._api_key is None:  # authenticate() either sets it or raises
            raise PluggyError("no API key after authentication")
        return self._api_key

    # --- transport ---

    def _send(self, method: Literal["GET", "POST"], url: str, **kw: Any) -> httpx.Response:
        """Send with 429 handling. Only GET and POST (for /auth) are allowed."""
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            resp = self._http.request(method, url, **kw)
            if resp.status_code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                return resp
            wait = resp.headers.get("RateLimit-Reset") or resp.headers.get("Retry-After") or "60"
            try:
                seconds = min(max(float(wait), 1.0), 60.0)
            except ValueError:
                seconds = 60.0
            log.warning("Pluggy rate limit hit; waiting %.0fs", seconds)
            self._sleep(seconds)
        raise AssertionError("unreachable")

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        resp = self._send("GET", url, params=params, headers={"X-API-KEY": self._key()})
        if resp.status_code in (401, 403):
            # The API key may have expired (2 h). UNVERIFIED: the docs do not say
            # which status an expired key returns; 401 is documented as "API key is
            # wrong". Re-authenticate once and retry.
            log.debug("Pluggy returned %s; re-authenticating once", resp.status_code)
            self._api_key = None
            resp = self._send("GET", url, params=params, headers={"X-API-KEY": self._key()})
        if resp.status_code != 200:
            raise _error_from(resp)
        return _decode(resp)

    # --- Provider protocol ---

    def get_item(self, item_id: str) -> Item:
        raw = _RawItem.model_validate(self._get(f"/items/{item_id}"))
        return Item(
            id=raw.id,
            connector_name=raw.connector.name if raw.connector else None,
            status=raw.status,
            consent_expires_at=raw.consentExpiresAt,
            last_updated_at=raw.lastUpdatedAt,
        )

    def list_items(self, item_ids: list[str]) -> list[Item]:
        # Pluggy does not list Meu Pluggy items (GET /v2/items is not available
        # for personal accounts), so the user provides the ids.
        return [self.get_item(i) for i in item_ids]

    def list_accounts(self, item_id: str) -> list[Account]:
        body = self._get("/accounts", params={"itemId": item_id})
        results = body.get("results", []) if isinstance(body, dict) else []
        accounts = []
        for r in results:
            raw = _RawAccount.model_validate(r)
            accounts.append(
                Account(
                    id=raw.id,
                    item_id=raw.itemId,
                    kind=account_kind(raw.subtype, raw.type),
                    name=raw.name or "",
                    number_masked=mask_number(raw.number),
                    currency=raw.currencyCode or "BRL",
                )
            )
        log.info("item %s: %d accounts", item_id, len(accounts))
        return accounts

    def list_transactions(
        self, account: Account, date_from: date, date_to: date | None = None
    ) -> Iterator[Transaction]:
        """All transactions of ``account`` from ``date_from``, following the cursor."""
        params = {"accountId": account.id, "dateFrom": date_from.isoformat()}
        if date_to is not None:
            params["dateTo"] = date_to.isoformat()
        url: str = TRANSACTIONS_PATH
        query: dict[str, str] | None = params
        seen: set[str] = set()
        pages = total = 0
        while True:
            body = self._get(url, params=query)
            if not isinstance(body, dict):
                raise PluggyError("unexpected /v2/transactions response shape")
            results = body.get("results") or []
            pages += 1
            total += len(results)
            for r in results:
                yield normalize_transaction(_RawTransaction.model_validate(r), account)
            nxt = body.get("next")
            if not nxt:
                break
            url, query = self._next_url(str(nxt)), None
            if url in seen:
                raise PluggyError("Pluggy repeated a pagination cursor; stopping")
            seen.add(url)
        log.info("account %s: %d transactions in %d pages", account.id, total, pages)

    def list_bills(self, account: Account) -> list[Bill]:
        """Closed bills of a credit card account (``GET /bills``, page pagination).

        Only closed bills exist in this endpoint; the open bill is estimated
        locally from transactions (``analytics/card_bill.py``).
        """
        bills: list[Bill] = []
        page = 1
        while True:
            body = self._get(
                "/bills",
                params={
                    "accountId": account.id,
                    "pageSize": str(BILLS_PAGE_SIZE),
                    "page": str(page),
                },
            )
            if not isinstance(body, dict):
                raise PluggyError("unexpected /bills response shape")
            for r in body.get("results") or []:
                bills.append(normalize_bill(_RawBill.model_validate(r), account.id))
            total_pages = int(body.get("totalPages") or 1)
            if page >= total_pages or page >= MAX_BILL_PAGES:
                break
            page += 1
        log.info("account %s: %d bills", account.id, len(bills))
        return bills

    def _next_url(self, nxt: str) -> str:
        """``next`` is a query string to append to the path as-is (docs: Basic concepts)."""
        if nxt.startswith("?"):
            return TRANSACTIONS_PATH + nxt
        parsed = httpx.URL(nxt)
        if parsed.is_absolute_url:
            if parsed.host != self._http.base_url.host:
                raise PluggyError("Pluggy 'next' points to another host; refusing")
            return parsed.raw_path.decode("ascii")
        raise PluggyError("unexpected 'next' format in /v2/transactions response")

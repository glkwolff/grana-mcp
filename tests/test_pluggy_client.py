"""HTTP behavior of the read-only Pluggy client, against a synthetic fake API."""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import pytest

import grana_mcp.providers.pluggy as pluggy_module
from grana_mcp.providers.base import Account
from grana_mcp.providers.pluggy import (
    API_KEY_TTL_SECONDS,
    PluggyAuthError,
    PluggyClient,
    PluggyError,
)
from tests.fakes import CARD_ID, CHECKING_ID, CURSOR_1, ITEM_ID, FakePluggy

FROM = date(2025, 11, 15)
CHECKING = Account(CHECKING_ID, ITEM_ID, "checking", "c", None, "BRL")
CARD = Account(CARD_ID, ITEM_ID, "credit_card", "k", None, "BRL")


def test_item_fields() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        item = c.get_item(ITEM_ID)
    assert item.connector_name == "MeuPluggy"
    assert item.status == "UPDATED"
    assert item.consent_expires_at is not None
    assert item.consent_expires_at.isoformat() == "2027-01-10T12:00:00+00:00"


def test_accounts_mapping() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        accounts = c.list_accounts(ITEM_ID)
    assert [(a.id, a.kind, a.number_masked) for a in accounts] == [
        (CHECKING_ID, "checking", "****00-0"),
        (CARD_ID, "credit_card", "****0000"),
    ]


def test_pagination_follows_next_cursor() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        txs = list(c.list_transactions(CHECKING, FROM))
    assert len(txs) == 4
    tx_requests = [r for r in fake.requests if r.url.path == "/v2/transactions"]
    assert len(tx_requests) == 2
    first, second = tx_requests
    assert first.url.params["dateFrom"] == "2025-11-15"
    assert first.url.params["accountId"] == CHECKING_ID
    assert "after" not in first.url.params
    # The second request is the path + `next` exactly as returned.
    assert second.url.params["after"] == CURSOR_1
    assert second.url.params["accountId"] == CHECKING_ID


def test_repeated_cursor_stops() -> None:
    fake = FakePluggy()
    page2 = fake.pages[CHECKING_ID][CURSOR_1]
    page2["next"] = f"?accountId={CHECKING_ID}&after={CURSOR_1}"
    with fake.client() as c, pytest.raises(PluggyError, match="repeated"):
        list(c.list_transactions(CHECKING, FROM))


def test_next_to_other_host_is_refused() -> None:
    fake = FakePluggy()
    fake.pages[CARD_ID][""]["next"] = "https://evil.example/v2/transactions?after=x"
    with fake.client() as c, pytest.raises(PluggyError, match="another host"):
        list(c.list_transactions(CARD, FROM))


def test_cents_are_exact_from_json_decimals() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        by_id = {t.id[-1]: t for t in c.list_transactions(CARD, FROM)}
    assert by_id["1"].amount_cents == -8990
    assert by_id["2"].amount_cents == 123456
    assert by_id["3"].amount_cents == -5537  # amountInAccountCurrency
    assert by_id["4"].amount_cents == 1999  # 19.99 would be 1998 via float
    assert (by_id["1"].installment_number, by_id["1"].installment_total) == (1, 3)
    assert by_id["2"].operation_type == "PAGAMENTO_FATURA"


def test_counterpart_is_receiver_for_outflow_and_payer_for_inflow() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        by_id = {t.id[-1]: t for t in c.list_transactions(CHECKING, FROM)}
    assert by_id["1"].counterpart_doc == "99999999999"  # Pix sent: receiver
    assert by_id["2"].counterpart_doc == "00000000000000"  # salary: payer CNPJ
    assert by_id["3"].counterpart_doc is None


def test_reauth_when_key_rejected() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        c.get_item(ITEM_ID)
        fake.expired_keys.add(fake.issued_keys[-1])
        c.get_item(ITEM_ID)
    assert len(fake.issued_keys) == 2
    assert fake.paths().count(("POST", "/auth")) == 2


def test_key_renewed_before_two_hours() -> None:
    fake = FakePluggy()
    now = [0.0]
    with fake.client(clock=lambda: now[0]) as c:
        c.get_item(ITEM_ID)
        now[0] = 60 * 60.0
        c.get_item(ITEM_ID)
        assert len(fake.issued_keys) == 1
        now[0] = float(API_KEY_TTL_SECONDS)
        c.get_item(ITEM_ID)
    assert len(fake.issued_keys) == 2


def test_bad_credentials() -> None:
    fake = FakePluggy()
    client = PluggyClient("wrong", "wrong", transport=fake.transport())
    with client, pytest.raises(PluggyAuthError):
        client.authenticate()


def test_rate_limit_waits_and_retries() -> None:
    fake = FakePluggy()
    waits: list[float] = []
    with fake.client(sleep=waits.append) as c:
        c.authenticate()
        fake.rate_limit_next = 2
        c.get_item(ITEM_ID)
    assert waits == [2.0, 2.0]


def test_http_errors_carry_code_not_body() -> None:
    fake = FakePluggy()
    with fake.client() as c, pytest.raises(PluggyError) as err:
        c.get_item("00000000-0000-4000-8000-00000000dead")
    assert err.value.status == 404
    assert err.value.code == "ITEM_NOT_FOUND"


# --- read-only guarantees ---


def test_only_get_and_post_auth_go_over_the_wire() -> None:
    fake = FakePluggy()
    with fake.client() as c:
        c.get_item(ITEM_ID)
        for account in c.list_accounts(ITEM_ID):
            list(c.list_transactions(account, FROM))
    for method, path in fake.paths():
        assert method == "GET" or (method, path) == ("POST", "/auth"), (method, path)


def test_client_exposes_no_write_methods() -> None:
    public = {n for n, _ in inspect.getmembers(PluggyClient) if not n.startswith("_")}
    assert public == {
        "authenticate",
        "close",
        "get_item",
        "list_items",
        "list_accounts",
        "list_transactions",
        "list_bills",
    }


ALLOWED_PATH_PREFIXES = ("/auth", "/items/", "/accounts", "/v2/transactions", "/bills")


def test_module_mentions_only_read_endpoints() -> None:
    source = Path(inspect.getfile(pluggy_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value in {"PATCH", "PUT", "DELETE"}:
                pytest.fail(f"HTTP verb literal {value!r} in the Pluggy client")
            if value.startswith("/") and not value.startswith(ALLOWED_PATH_PREFIXES):
                pytest.fail(f"unexpected endpoint literal {value!r}")
    for forbidden in ("connect_token", "/payments", "/pix", "smart-transfer", "/mfa"):
        assert forbidden not in source.lower()

"""Contract tests against the real Pluggy API using Sandbox credentials.

Skipped unless ``GRANA_PLUGGY_SANDBOX_CLIENT_ID`` and
``GRANA_PLUGGY_SANDBOX_CLIENT_SECRET`` are set. Item-level checks also need
``GRANA_PLUGGY_SANDBOX_ITEM_ID``: create a Sandbox ("Pluggy Bank") item in the
Pluggy Dashboard (user-ok / password-ok). This client never creates items.
Assertions are about shape only, not values (the Sandbox data changes).
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from grana_mcp.providers.pluggy import PluggyClient

CLIENT_ID = os.environ.get("GRANA_PLUGGY_SANDBOX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GRANA_PLUGGY_SANDBOX_CLIENT_SECRET")
ITEM_ID = os.environ.get("GRANA_PLUGGY_SANDBOX_ITEM_ID")

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not (CLIENT_ID and CLIENT_SECRET), reason="Pluggy Sandbox credentials not set"
    ),
]


@pytest.fixture
def client() -> PluggyClient:
    assert CLIENT_ID and CLIENT_SECRET
    return PluggyClient(CLIENT_ID, CLIENT_SECRET)


def test_auth(client: PluggyClient) -> None:
    with client:
        client.authenticate()


@pytest.mark.skipif(not ITEM_ID, reason="GRANA_PLUGGY_SANDBOX_ITEM_ID not set")
def test_item_accounts_transactions_shape(client: PluggyClient) -> None:
    assert ITEM_ID
    with client:
        item = client.get_item(ITEM_ID)
        assert item.id == ITEM_ID
        accounts = client.list_accounts(ITEM_ID)
        assert accounts
        for account in accounts:
            assert account.kind in ("checking", "credit_card", "other")
            since = date.today() - timedelta(days=365)
            for tx in client.list_transactions(account, since):
                assert isinstance(tx.amount_cents, int)
                assert tx.account_id == account.id

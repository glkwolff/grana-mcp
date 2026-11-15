from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from grana_mcp import cli, config
from grana_mcp.providers.pluggy import PluggyClient
from tests.conftest import MemoryKeyring
from tests.fakes import CLIENT_ID, CLIENT_SECRET, ITEM_ID, FakePluggy

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePluggy:
    fake = FakePluggy()

    def make_client(creds: config.Credentials) -> PluggyClient:
        return PluggyClient(
            creds.client_id, creds.client_secret, transport=fake.transport(), sleep=lambda _: None
        )

    monkeypatch.setattr(cli, "make_client", make_client)
    return fake


def _add_profile(secret: str = CLIENT_SECRET) -> str:
    result = runner.invoke(
        cli.app,
        ["profile", "add", "--display-name", "Ana Sintética", "--item-id", ITEM_ID],
        input=f"{CLIENT_ID}\n{secret}\n",
    )
    return result.output


def test_profile_add_stores_secret_in_keyring(fake: FakePluggy, isolated: MemoryKeyring) -> None:
    out = _add_profile()
    assert "MeuPluggy" in out
    assert CLIENT_SECRET not in out
    assert config.load_credentials("ana-sintetica").client_secret == CLIENT_SECRET
    assert ("grana-mcp", "ana-sintetica") in isolated.store
    # The config file holds item ids but no secret.
    text = config.config_path().read_text()
    assert ITEM_ID in text
    assert CLIENT_SECRET not in text
    assert CLIENT_ID not in text


def test_profile_add_rejects_bad_credentials(fake: FakePluggy) -> None:
    out = _add_profile(secret="wrong")
    assert "recusou" in out
    assert config.load_profiles() == []


def test_sync_then_status(fake: FakePluggy) -> None:
    _add_profile()
    first = runner.invoke(cli.app, ["sync"])
    assert first.exit_code == 0, first.output
    assert "8 novas" in first.output
    second = runner.invoke(cli.app, ["sync", "--profile", "ana-sintetica"])
    assert "0 novas, 0 atualizadas, 0 removidas" in second.output

    status = runner.invoke(cli.app, ["status"])
    out = status.output
    assert status.exit_code == 0, out
    assert "autorização expira em 2027-01-10" in out
    assert "Conta Corrente Sintetica ****00-0 [checking]" in out
    assert "2026-10  Conta Corrente Sintetica [checking]: R$ 1.384,81 (2 transações)" in out
    assert "2026-09  Conta Corrente Sintetica [checking]: R$ 10,10 (1 transações)" in out
    assert "2026-10  Cartao Sintetico [credit_card]: R$ 145,27 (2 transações)" in out
    for text in (first.output, second.output, out):
        assert CLIENT_SECRET not in text
        assert "synthetic-api-key" not in text


def test_sync_uses_env_credentials_without_keyring(
    fake: FakePluggy, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.upsert_profile(config.Profile(id="bia", display_name="Bia", item_ids=[ITEM_ID]))
    monkeypatch.setenv(config.ENV_CLIENT_ID, CLIENT_ID)
    monkeypatch.setenv(config.ENV_CLIENT_SECRET, CLIENT_SECRET)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output


def test_sync_without_credentials_fails_cleanly(fake: FakePluggy) -> None:
    config.upsert_profile(config.Profile(id="bia", display_name="Bia", item_ids=[ITEM_ID]))
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    assert "no Pluggy credentials" in result.output


def test_status_warns_when_consent_is_near(fake: FakePluggy) -> None:
    soon = datetime.now(UTC) + timedelta(days=10, hours=1)
    fake.item["consentExpiresAt"] = soon.isoformat().replace("+00:00", "Z")
    _add_profile()
    runner.invoke(cli.app, ["sync"])
    out = runner.invoke(cli.app, ["status"]).output
    assert "ATENÇÃO" in out
    assert "(10 dias)" in out


@pytest.mark.parametrize(
    ("days", "warn", "snippet"),
    [
        (31, False, "(31 dias)"),
        (30, True, "ATENÇÃO"),
        (1, True, "ATENÇÃO"),
        (-1, True, "EXPIROU"),
    ],
)
def test_consent_line(days: int, warn: bool, snippet: str) -> None:
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    expires = (now + timedelta(days=days, minutes=1)).isoformat()
    text, flagged = cli.consent_line(expires, now)
    assert flagged is warn
    assert snippet in text


def test_consent_line_without_date() -> None:
    assert cli.consent_line(None, datetime.now(UTC)) == (
        "autorização: sem data de expiração",
        False,
    )

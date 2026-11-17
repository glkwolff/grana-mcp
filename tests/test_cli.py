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
    # The R$ 1.234,56 bill payment is excluded: the paid card is synced (same item).
    assert "2026-10  Conta Corrente Sintetica [checking]: R$ 150,25 (1 transações)" in out
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


# --- people, ownership, household documents, reprocess ---


def _synced(fake: FakePluggy) -> None:
    _add_profile()
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "limpeza: 2 pagamento(s) de fatura" in result.output


def test_person_and_account_commands(fake: FakePluggy) -> None:
    _synced(fake)
    assert "cada perfil conta como uma pessoa" in runner.invoke(cli.app, ["person", "list"]).output
    out = runner.invoke(cli.app, ["person", "add", "Bia Sintética"])
    assert out.exit_code == 0 and "bia-sintetica" in out.output
    dup = runner.invoke(cli.app, ["person", "add", "Bia Sintética"])
    assert dup.exit_code == 1 and "já existe" in dup.output

    listed = runner.invoke(cli.app, ["account", "list"]).output
    assert "sem dono" in listed and "****00-0" in listed

    ok = runner.invoke(cli.app, ["account", "assign", "00-0", "bia sintética"])
    assert ok.exit_code == 0, ok.output
    assert "-> bia-sintetica" in ok.output and "limpeza:" in ok.output
    amb = runner.invoke(cli.app, ["account", "assign", "0", "bia-sintetica"])
    assert amb.exit_code == 1 and "mais de uma conta" in amb.output
    missing = runner.invoke(cli.app, ["account", "assign", "zzzz", "bia-sintetica"])
    assert missing.exit_code == 1 and "não encontrada" in missing.output
    nobody = runner.invoke(cli.app, ["account", "assign", "00-0", "ninguem"])
    assert nobody.exit_code == 1 and "não existe" in nobody.output

    people = runner.invoke(cli.app, ["person", "list"]).output
    assert "Bia Sintética: 1 conta(s), 0 documento(s)" in people
    status = runner.invoke(cli.app, ["status"]).output
    assert "Pessoas: Bia Sintética (bia-sintetica)" in status
    assert "; Bia Sintética" in status


def test_household_add_doc(fake: FakePluggy) -> None:
    from tests.test_normalize_documents import SYNTH_CPF

    _synced(fake)
    runner.invoke(cli.app, ["person", "add", "Ana"])
    ok = runner.invoke(cli.app, ["household", "add-doc", "ana"], input=f"{SYNTH_CPF}\n")
    assert ok.exit_code == 0, ok.output
    assert "CPF registrado para 'ana'" in ok.output
    assert SYNTH_CPF not in ok.output
    again = runner.invoke(cli.app, ["household", "add-doc", "ana"], input=f"{SYNTH_CPF}\n")
    assert "já existia" in again.output
    bad = runner.invoke(cli.app, ["household", "add-doc", "ana"], input="123\n")
    assert bad.exit_code == 1 and "inválido" in bad.output
    # No person with that name, but a profile: stored for the profile.
    prof = runner.invoke(cli.app, ["household", "add-doc", "ana-sintetica"], input=f"{SYNTH_CPF}\n")
    assert prof.exit_code == 0 and "'ana-sintetica'" in prof.output
    unknown = runner.invoke(cli.app, ["household", "add-doc", "ninguem"], input=f"{SYNTH_CPF}\n")
    assert unknown.exit_code == 1
    raw = config.db_path().read_bytes()
    assert SYNTH_CPF.encode() not in raw


def test_reprocess_without_network(fake: FakePluggy) -> None:
    _synced(fake)
    n_requests = len(fake.requests)
    out = runner.invoke(cli.app, ["reprocess"])
    assert out.exit_code == 0, out.output
    assert "0 alteração(ões)" in out.output
    assert "1 estorno(s) sem compra correspondente" in out.output
    assert len(fake.requests) == n_requests


def test_sync_no_identity_flag(fake: FakePluggy) -> None:
    _add_profile()
    out = runner.invoke(cli.app, ["sync", "--no-identity"])
    assert out.exit_code == 0, out.output
    assert not any(r.url.path == "/identity" for r in fake.requests)
    fake.identity = {"document": "0" * 11}
    out = runner.invoke(cli.app, ["sync"])
    assert "1 documento(s) de titular aprendido(s)" in out.output


def test_doctor_online(fake: FakePluggy, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_profile()
    runner.invoke(cli.app, ["sync"])
    monkeypatch.setenv(config.ENV_CLIENT_SECRET, "x")
    result = runner.invoke(cli.app, ["doctor"])
    assert "credenciais encontradas" in result.output
    assert "autorização expira em" in result.output
    assert "prefira o keyring" in result.output
    assert "último sync ok" in result.output
    assert CLIENT_SECRET not in result.output


def test_init_wizard(fake: FakePluggy, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(
        cli.app,
        ["init"],
        input=f"y\ny\nAna Sintética\n{CLIENT_ID}\n{CLIENT_SECRET}\n{ITEM_ID}\n",
    )
    assert result.exit_code == 0, result.output
    assert opened == [cli.MEU_PLUGGY_URL, cli.DASHBOARD_URL]
    assert "8 novas" in result.output
    assert "claude mcp add grana -- uvx grana-mcp serve" in result.output
    assert CLIENT_SECRET not in result.output


def test_sync_if_updated_only_when_pluggy_refreshed(fake: FakePluggy) -> None:
    from grana_mcp.sync import changed_items

    _add_profile()
    first = runner.invoke(cli.app, ["sync", "--if-updated"])  # never synced: full sync
    assert first.exit_code == 0, first.output
    assert "8 novas" in first.output

    fake.requests.clear()
    quiet = runner.invoke(cli.app, ["sync", "--if-updated"])  # nothing new at Pluggy
    assert quiet.exit_code == 0, quiet.output
    assert quiet.output == ""
    paths = {r.url.path for r in fake.requests}
    assert paths <= {"/auth", f"/items/{ITEM_ID}"}  # one light look per item

    later = datetime.now(UTC) + timedelta(minutes=1)
    fake.item["lastUpdatedAt"] = later.isoformat().replace("+00:00", "Z")
    again = runner.invoke(cli.app, ["sync", "--if-updated"])  # Meu Pluggy refreshed
    assert "1 conexão(ões) atualizada(s)" in again.output
    assert "0 novas" in again.output

    # A day without any refresh still gets a full sync.
    fake.item["lastUpdatedAt"] = "2026-11-14T06:00:00.000Z"
    store = cli.open_store()
    try:
        (profile,) = config.load_profiles()
        client = cli.make_client(config.load_credentials(profile.id))
        with client:
            assert changed_items(store, client, profile) == []
            tomorrow = datetime.now(UTC) + timedelta(hours=25)
            assert changed_items(store, client, profile, tomorrow) == [ITEM_ID]
    finally:
        store.conn.close()

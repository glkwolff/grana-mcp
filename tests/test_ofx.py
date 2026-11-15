from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from grana_mcp import cli, config
from grana_mcp.providers import ofx
from grana_mcp.store import db

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
runner = CliRunner()


def test_parse_ofx_sgml_checking() -> None:
    stmt = ofx.parse_ofx(ofx.read_text(FIXTURES / "extrato_conta.ofx"), "ana")
    assert stmt.account.kind == "checking"
    assert stmt.account.number_masked == "****34-5"
    assert stmt.account.id.startswith("file:")
    amounts = [t.amount_cents for t in stmt.transactions]
    assert amounts == [-12345, 500000, -123456]
    assert stmt.transactions[0].posted_date.isoformat() == "2026-10-05"
    assert stmt.transactions[0].description_raw == "COMPRA SUPERMERCADO SINTETICO"
    again = ofx.parse_ofx(ofx.read_text(FIXTURES / "extrato_conta.ofx"), "ana")
    assert [t.id for t in again.transactions] == [t.id for t in stmt.transactions]


def test_parse_ofx_xml_credit_card() -> None:
    stmt = ofx.parse_ofx(ofx.read_text(FIXTURES / "cartao.ofx"), "ana", "Cartao X")
    assert stmt.account.kind == "credit_card"
    assert stmt.account.name == "Cartao X"
    assert [t.amount_cents for t in stmt.transactions] == [-4590, 30000]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nada aqui", "OFX"),
        ("<OFX><BANKTRANLIST></BANKTRANLIST></OFX>", "nenhuma"),
        ("<OFX><STMTTRN><DTPOSTED>x<TRNAMT>1</STMTTRN></OFX>", "data"),
        ("<OFX><STMTTRN><DTPOSTED>20261001<TRNAMT>abc</STMTTRN></OFX>", "valor"),
    ],
)
def test_parse_ofx_errors(text: str, message: str) -> None:
    with pytest.raises(ofx.ImportFormatError, match=message):
        ofx.parse_ofx(text, "ana")


NUBANK_CARTAO = (
    "date,title,amount\n"
    '2026-11-05,Loja Sintetica,"215,05"\n'
    '2026-11-05,Loja Sintetica,"215,05"\n'
    '2026-11-03,Pagamento recebido,"- 90,00"\n'
)
NUBANK_CONTA = (
    "Data,Valor,Identificador,Descrição\n"
    "01/10/2026,-50.00,abc-1,Compra no débito - Mercado Sintetico\n"
    "02/10/2026,1500.00,abc-2,Transferência recebida\n"
)
GENERIC = "date,description,amount\n2026-10-01,Algo,-10.5\n2026-10-02,Outro,1.234,00\n"


def test_parse_csv_formats() -> None:
    card = ofx.parse_csv(NUBANK_CARTAO, "ana", "nubank-cartao")
    assert card.account.kind == "credit_card"
    assert [t.amount_cents for t in card.transactions] == [-21505, -21505, 9000]
    # Identical rows are kept as two transactions with different ids.
    assert len({t.id for t in card.transactions}) == 3
    conta = ofx.parse_csv(NUBANK_CONTA, "ana", "nubank-conta")
    assert [t.amount_cents for t in conta.transactions] == [-5000, 150000]
    assert conta.transactions[0].posted_date.isoformat() == "2026-10-01"
    inverted = ofx.parse_csv(NUBANK_CONTA, "ana", "nubank-conta", invert=True)
    assert inverted.transactions[0].amount_cents == 5000


def test_parse_csv_errors() -> None:
    with pytest.raises(ofx.ImportFormatError, match="colunas"):
        ofx.parse_csv(NUBANK_CONTA, "ana", "nubank-cartao")
    with pytest.raises(ofx.ImportFormatError, match="linhas"):
        ofx.parse_csv("date,title,amount\n", "ana", "nubank-cartao")
    with pytest.raises(ofx.ImportFormatError, match="data"):
        ofx.parse_csv("date,title,amount\n31/31/2026,x,1\n", "ana", "nubank-cartao")
    with pytest.raises(ofx.ImportFormatError, match="desconhecido"):
        ofx.parse_csv(GENERIC, "ana", "outro")  # type: ignore[arg-type]


def test_read_text_cp1252(tmp_path: Path) -> None:
    f = tmp_path / "x.csv"
    f.write_bytes("date,description,amount\n2026-10-01,Pão,-1\n".encode("cp1252"))
    assert "Pão" in ofx.read_text(f)


def test_cli_import_ofx_is_idempotent_and_cleans(tmp_path: Path) -> None:
    runner.invoke(cli.app, ["person", "add", "Ana"])
    first = runner.invoke(
        cli.app,
        ["import-ofx", str(FIXTURES / "cartao.ofx"), "--profile", "ana", "--person", "ana"],
    )
    assert first.exit_code == 0, first.output
    assert "2 novas" in first.output
    second = runner.invoke(
        cli.app, ["import-ofx", str(FIXTURES / "cartao.ofx"), "--profile", "ana"]
    )
    assert "0 novas, 0 atualizadas" in second.output
    conn = db.connect(config.db_path())
    rows = dict(
        conn.execute("SELECT description_raw, excluded_reason FROM transactions").fetchall()
    )
    owner = conn.execute("SELECT owner_person_id FROM accounts").fetchone()[0]
    sources = {r[0] for r in conn.execute("SELECT source FROM transactions")}
    conn.close()
    # "Pagamento recebido" on a card is the bill payment, not income.
    assert rows["Pagamento recebido"] == "card_bill_payment"
    assert owner == "ana"
    assert sources == {"ofx"}


def test_cli_import_csv_and_errors(tmp_path: Path) -> None:
    f = tmp_path / "nubank.csv"
    f.write_text(NUBANK_CARTAO, encoding="utf-8")
    ok = runner.invoke(
        cli.app, ["import-csv", str(f), "--profile", "ana", "--format", "nubank-cartao"]
    )
    assert ok.exit_code == 0, ok.output
    assert "3 novas" in ok.output
    bad_fmt = runner.invoke(cli.app, ["import-csv", str(f), "--profile", "ana", "--format", "x"])
    assert bad_fmt.exit_code == 1
    bad = runner.invoke(cli.app, ["import-csv", str(f), "--profile", "ana", "--format", "generic"])
    assert bad.exit_code == 1
    assert "colunas" in bad.output
    missing = runner.invoke(cli.app, ["import-ofx", str(tmp_path / "nao.ofx"), "--profile", "ana"])
    assert missing.exit_code == 1


def test_doctor_offline_and_without_db() -> None:
    result = runner.invoke(cli.app, ["doctor", "--offline"])
    assert result.exit_code == 1
    assert "banco de dados não existe" in result.output
    runner.invoke(cli.app, ["person", "add", "Ana"])  # creates the DB
    result = runner.invoke(cli.app, ["doctor", "--offline"])
    assert "permissões do banco: 0o600" in result.output
    assert "schema v" in result.output

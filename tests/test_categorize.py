from __future__ import annotations

import sqlite3
import tomllib
from collections.abc import Iterator
from importlib import resources

import pytest

from grana_mcp import config
from grana_mcp.pipeline import categorize as cat
from grana_mcp.store import db


def tx(
    desc: str,
    provider_category: str | None = None,
    *,
    merchant: str | None = None,
    op: str | None = None,
    doc_type: str | None = None,
) -> cat.TxText:
    return cat.TxText(desc, merchant, provider_category, op, doc_type)


BUILTIN = cat.load_builtin_rules()


def categorize(t: cat.TxText, user: list[cat.Rule] | None = None) -> str:
    return cat.categorize(t, user or [], BUILTIN)


# --- built-in rules file ---


def test_builtin_rules_file_is_valid_and_large_enough() -> None:
    text = resources.files("grana_mcp.rules").joinpath("categories_br.toml").read_text("utf-8")
    data = tomllib.loads(text)
    assert sum(len(r["patterns"]) for r in data["rule"]) >= 150
    for block in data["rule"]:
        assert block["category"] in cat.TAXONOMY
    assert len(BUILTIN) >= 150


@pytest.mark.parametrize(
    ("desc", "pluggy", "expected"),
    [
        ("Compra no débito|SUPERMERCADO SINTETICO", "Shopping", "Mercado"),
        ("Dl *Uberrides", "Taxi and ride-hailing", "Transporte"),
        ("99food *Lanchonete Sintetica", "Food delivery", "Delivery"),
        ("Transferência enviada pelo Pix|99 FOOD LTDA", "Services", "Delivery"),
        ("Compra no débito via NuPay|99", "Services", "Transporte"),
        ("Pagamento efetuado|IMOVEIS SINTETICOS LTDA", "Services", "Moradia"),
        ("EBW*Spotify", "Music streaming", "Assinaturas"),
        ("Raia2153 2/3", "Pharmacy", "Farmácia"),
        ("Amazonmktplc*Lojasintetica 3/12", "Electronics", "Compras"),
        ("Pagamento de Pix QR Code SHPP BRASIL INSTITUICAO", "Services", "Compras"),
        ("Pagamento efetuado|COPEL DISTRIBUICAO", "Transfers", "Contas"),
    ],
)
def test_builtin_rules_beat_vague_pluggy_categories(desc: str, pluggy: str, expected: str) -> None:
    assert categorize(tx(desc, pluggy)) == expected


def test_whole_word_matching_avoids_false_positives() -> None:
    # "mercado" must not catch "mercadolivre"; "gas" is not a rule (would catch "gastronomia").
    assert categorize(tx("Mercadolivre*Lojax", None)) == "Compras"
    assert categorize(tx("GASTRONOMIA SINTETICA", None)) == "Outros"


def test_pluggy_mapping_then_person_transfer_then_fallback() -> None:
    assert categorize(tx("LOJA DESCONHECIDA", "Groceries")) == "Mercado"
    assert categorize(tx("LOJA DESCONHECIDA", "Services")) == "Outros"
    assert categorize(tx("Pix enviado FULANO", "Services", op="PIX", doc_type="CPF")) == (
        "Transferências"
    )
    assert categorize(tx("Pix enviado EMPRESA", "Services", op="PIX", doc_type="CNPJ")) == "Outros"
    assert categorize(tx("QUALQUER", None)) == "Outros"


def test_all_mapped_pluggy_categories_are_in_taxonomy() -> None:
    for value in cat.PLUGGY_MAP.values():
        assert value is None or value in cat.TAXONOMY


def test_user_rules_take_precedence() -> None:
    user = [cat.make_rule("supermercado sintetico", "Restaurantes")]
    assert categorize(tx("Compra no débito|SUPERMERCADO SINTETICO", "Groceries"), user) == (
        "Restaurantes"
    )
    regex = [cat.make_rule(r"^padaria\s+x", "Mercado", "regex")]
    assert categorize(tx("Padaria X 123", None), regex) == "Mercado"
    merchant = [cat.make_rule("Loja Y", "Pets", "merchant")]
    assert categorize(tx("Compra no débito|LOJA Y", None), merchant) == "Pets"
    assert categorize(tx("Compra no débito|LOJA YZ", None), merchant) == "Outros"


@pytest.mark.parametrize(
    ("pattern", "match_type"),
    [
        ("ab", "contains"),
        ("", "contains"),
        ("(", "regex"),
        (".*", "regex"),
        ("x" * 200, "contains"),
    ],
)
def test_invalid_user_rules(pattern: str, match_type: cat.MatchType) -> None:
    with pytest.raises(cat.RuleError):
        cat.make_rule(pattern, "Mercado", match_type)


def test_invalid_match_type_and_category() -> None:
    with pytest.raises(cat.RuleError):
        cat.make_rule("loja", "Mercado", "fuzzy")  # type: ignore[arg-type]
    with pytest.raises(cat.RuleError):
        cat.make_rule("loja", "")


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        ("Compra no débito|LEROY SINTETICO", "LEROY SINTETICO"),
        ("Compra débito Loja Sintetica", "Loja Sintetica"),
        ("Pix enviado Empresa Sintetica", "Empresa Sintetica"),
        ("Dl *Uberrides", "Uberrides"),
        ("DM*Spotify P46FB", "Spotify P46FB"),
        ("Amazon - Parcela 1/4", "Amazon"),
        ("Loja Sintetica 1/6", "Loja Sintetica"),
        ("iFood - NuPay", "iFood"),
        ("Pagamento de Pix QR Code LOJA X", "LOJA X"),
    ],
)
def test_clean_merchant(desc: str, expected: str) -> None:
    assert cat.clean_merchant(desc) == expected


def test_clean_merchant_empty() -> None:
    assert cat.clean_merchant(None) is None
    assert cat.clean_merchant("") is None


# --- database round trip ---


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO profiles VALUES ('ana', 'Ana', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('item-a', 'ana');"
        "INSERT INTO accounts (id, item_id, kind, name) VALUES ('chk', 'item-a', 'checking', 'C');"
    )
    rows = [
        ("t1", "Compra no débito|PADARIA SINTETICA", "Shopping"),
        ("t2", "Dl *Uberrides", "Taxi and ride-hailing"),
        ("t3", "LOJA DESCONHECIDA", "Services"),
    ]
    for tx_id, desc, pc in rows:
        c.execute(
            "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
            "description_raw, provider_category, source, synced_at) "
            "VALUES (?, 'chk', 'ana', '2026-10-01', -1000, ?, ?, 'pluggy', 'x')",
            (tx_id, desc, pc),
        )
    c.commit()
    yield c
    c.close()


def categories(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT id, category FROM transactions").fetchall())


def test_run_categorize_is_idempotent(conn: sqlite3.Connection) -> None:
    first = cat.run_categorize(conn)
    assert first.changed == 3
    assert categories(conn) == {"t1": "Mercado", "t2": "Transporte", "t3": "Outros"}
    clean = conn.execute("SELECT description_clean FROM transactions WHERE id='t2'").fetchone()
    assert clean[0] == "Uberrides"
    assert cat.run_categorize(conn).changed == 0


def test_user_rule_add_list_delete(conn: sqlite3.Connection) -> None:
    cat.run_categorize(conn)
    rule = cat.add_user_rule(conn, "loja desconhecida", "Pets")
    assert rule.rule_id is not None
    cat.run_categorize(conn)
    assert categories(conn)["t3"] == "Pets"
    assert [r.rule_id for r in cat.list_user_rules(conn)] == [rule.rule_id]
    assert cat.delete_user_rule(conn, rule.rule_id)
    assert not cat.delete_user_rule(conn, rule.rule_id)
    cat.run_categorize(conn)
    assert categories(conn)["t3"] == "Outros"


def test_invalid_stored_user_rule_is_ignored(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO category_rules (pattern, match_type, category, priority, origin) "
        "VALUES ('(', 'regex', 'Pets', 0, 'user')"
    )
    conn.commit()
    assert cat.load_user_rules(conn) == []


@pytest.mark.parametrize(
    "variants",
    [
        ["99 FOOD LTDA", "99Food", "99 FOOD", "99 FOOD LTDA."],
        ["IFOOD", "iFood", "IFOOD.COM AGENCIA DE RESTAURANTES ONLINE S.A."],
    ],
)
def test_merchant_group_key_unifies_variants(variants: list[str]) -> None:
    from grana_mcp.analytics.queries import merchant_group_key

    assert len({merchant_group_key(v) for v in variants}) == 1

"""Groceries from receipts: lines kept, products grouped by kind, package contents, prices
per unit/kg and what changed between purchases. All receipts here are synthetic."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from grana_mcp import config
from grana_mcp.kitchen import groceries, prices, store
from grana_mcp.kitchen.service import KitchenTools
from tests.test_server import add_tx
from tests.test_server import conn as conn

TODAY = date(2026, 11, 18)
KEY_1 = "41261112345678000100650010000001231000000010"
KEY_2 = "41261112345678000100650010000001241000000011"


def qr(key: str) -> str:
    return f"http://www.fazenda.pr.gov.br/nfce/qrcode?p={key}|2|1|1|ABC123DEF"


def item(n: int, descr: str, qty: str, unit: str, price: str, total: str) -> str:
    return (
        f'<tr id="Item + {n}"><td valign="top"><span class="txtTit2">{descr}</span>'
        f'<span class="Rqtd"><strong>Qtde.:</strong>{qty}</span>'
        f'<span class="RUN"><strong>UN: </strong>{unit}</span>'
        f'<span class="RvlUnit"><strong>Vl. Unit.:</strong>&nbsp;{price}</span></td>'
        f'<td class="txtTit3"><span class="valor">{total}</span></td></tr>'
    )


def page(day: str, rows: list[str], total: str) -> str:
    return (
        '<div class="txtTopo">SUPERMERCADO EXEMPLO LTDA</div>'
        '<div class="text">CNPJ: 12.345.678/0001-00</div>'
        '<div class="text">RUA DE TESTE, 100, CENTRO, CURITIBA, PR</div>'
        '<table id="tabResult">' + "".join(rows) + "</table>"
        f'<span class="totalNumb txtMax">{total}</span>'
        f"<strong> Emissão: </strong>{day} 18:30:12 - Via Consumidor"
    )


PAGES = {
    KEY_1: page(
        "02/10/2026",
        [
            item(1, "ARROZ T1 TIO EXEMPLO 5KG", "1", "UN", "24,90", "24,90"),
            item(2, "TOMATE ITALIANO", "0,500", "KG", "8,00", "4,00"),
            item(3, "FRALDA EXEMPLO XG 116", "1", "UN", "89,90", "89,90"),
            item(4, "REFRI COLA EXEMPLO 2L", "2", "UN", "9,00", "18,00"),
        ],
        "136,80",
    ),
    KEY_2: page(
        "05/11/2026",
        [
            item(1, "ARROZ T1 TIO EXEMPLO 5KG", "1", "UN", "27,90", "27,90"),
            item(2, "TOMATE ITALIANO", "1,000", "KG", "7,00", "7,00"),
            item(3, "FRALDA EXEMPLO XG 116", "2", "UN", "99,90", "199,80"),
            item(4, "REFRI COLA EXEMPLO ZERO 2L", "1", "UN", "9,50", "9,50"),
            item(5, "DETERGENTE EXEMPLO", "1", "UN", "2,50", "2,50"),
        ],
        "246,70",
    ),
}


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prices, "now_utc", lambda: "2026-11-18T13:00:00+00:00")


def kitchen() -> KitchenTools:
    def fetch(url: str) -> str:
        key = url.split("p=", 1)[1][:44]
        return PAGES[key]

    return KitchenTools(config.db_path(), lambda: TODAY, lambda: "ana", receipts=fetch)


SUGGESTED = [
    {"product": "ARROZ T1 TIO EXEMPLO 5KG", "category": "mercearia", "size": None, "unit": None},
    {"product": "TOMATE ITALIANO", "category": "hortifruti", "size": None, "unit": None},
    {"product": "FRALDA EXEMPLO XG 116", "category": "bebe", "size": 116, "unit": "un"},
    {"product": "REFRI COLA EXEMPLO 2L", "category": "doces", "size": None, "unit": None},
]


def test_receipt_lines_are_kept_and_new_products_go_to_review(conn: sqlite3.Connection) -> None:
    k = kitchen()
    out = k.read_receipt(qr(KEY_1))
    assert conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0] == 4
    # Every product is new; only the diaper's content can't be read from the text.
    todo = {t["descr"]: t["needs_size"] for t in out["to_categorize"]}
    assert todo == {
        "ARROZ T1 TIO EXEMPLO 5KG": False,
        "TOMATE ITALIANO": False,
        "FRALDA EXEMPLO XG 116": True,
        "REFRI COLA EXEMPLO 2L": False,
    }
    assert out["groups"] == [{"category": groceries.UNCATEGORIZED, "total": "R$ 136,80", "new": []}]
    lines = {line["what"]: line for line in out["lines"]}
    assert lines["ARROZ T1 TIO EXEMPLO 5KG"]["per"] == "R$ 4,98/kg"
    assert lines["TOMATE ITALIANO"]["per"] == "R$ 8,00/kg"
    assert lines["FRALDA EXEMPLO XG 116"]["per"] is None  # content unknown yet

    view = k.suggest_products(out["ref"], SUGGESTED)
    groups = {g["category"]: g for g in view["groups"]}
    assert groups["Bebê"]["total"] == "R$ 89,90"
    assert groups["Bebê"]["new"] == ["FRALDA EXEMPLO XG 116"]
    diaper = next(x for x in view["lines"] if x["what"].startswith("FRALDA"))
    assert diaper["content"] == "116 un" and diaper["per"] == "R$ 0,775/un"
    # The shopping list's receipt prices learn the content too.
    (size, unit) = conn.execute(
        "SELECT size, unit FROM price_obs WHERE descr = 'FRALDA EXEMPLO XG 116'"
    ).fetchone()
    assert (size, unit) == (116, "un")

    review = k.product_review()
    assert review["suggested_count"] == 4
    assert review["suggested"]["Bebê"] == ["FRALDA EXEMPLO XG 116"]
    assert review["suggested_sizes"] == [{"product": "FRALDA EXEMPLO XG 116", "content": "116 un"}]


def test_household_corrections_win_over_suggestions(conn: sqlite3.Connection) -> None:
    k = kitchen()
    out = k.read_receipt(qr(KEY_1))
    k.suggest_products(out["ref"], SUGGESTED)
    # 'refri cola' reaches the product by its words; the household's word is final.
    fixed = k.set_product_category([{"product": "refri cola", "category": "bebidas"}])
    assert fixed["changed"] == [
        {"product": "REFRI COLA EXEMPLO 2L", "category": "Bebidas sem álcool"}
    ]
    again = k.set_product_category(
        [{"product": "REFRI COLA EXEMPLO 2L", "category": "doces"}], confirm=False
    )
    assert again["changed"] == []
    (cat, status) = conn.execute(
        "SELECT category, status FROM product_categories WHERE product LIKE 'refri%'"
    ).fetchone()
    assert (cat, status) == ("bebidas", "confirmed")

    size = k.set_product_size([{"product": "fralda", "qty": 120, "unit": "un"}])
    assert size["changed"] == [{"product": "FRALDA EXEMPLO XG 116", "size": "120 un"}]
    assert k.set_product_category([{"product": "pão de queijo", "category": "padaria"}])[
        "not_found"
    ] == ["pão de queijo"]
    with pytest.raises(store.KitchenError, match="categoria desconhecida"):
        k.set_product_category([{"product": "refri", "category": "brinquedos"}])
    with pytest.raises(store.KitchenError, match="tamanho inválido"):
        k.set_product_size([{"product": "fralda", "qty": 0, "unit": "un"}])

    assert "3 categoria(s) e 0" in k.confirm_products()["summary"]
    assert k.product_review()["suggested_count"] == 0


def test_known_products_are_grouped_by_themselves_and_prices_compared(
    conn: sqlite3.Connection,
) -> None:
    k = kitchen()
    first = k.read_receipt(qr(KEY_1))
    k.suggest_products(first["ref"], SUGGESTED)
    k.confirm_products()

    second = k.read_receipt(qr(KEY_2))
    # Only the products never seen go to the model.
    assert [t["descr"] for t in second["to_categorize"]] == [
        "REFRI COLA EXEMPLO ZERO 2L",
        "DETERGENTE EXEMPLO",
    ]
    groups = {g["category"]: g["total"] for g in second["groups"]}
    assert groups["Bebê"] == "R$ 199,80" and groups["Hortifrúti"] == "R$ 7,00"

    paid = add_tx(conn, "2026-11-06", -246_70, "SUPERMERCADO EXEMPLO", category="Mercado")
    add_tx(conn, "2026-11-10", -150_00, "PADARIA SINTETICA", category="Mercado")
    conn.commit()
    out = k.grocery_spending("2026-11")
    assert out["receipts"] == 1 and out["total"] == "R$ 246,70"
    assert out["previous_total"] == "R$ 136,80"  # October
    assert conn.execute("SELECT COUNT(*) FROM receipts WHERE txn_id = ?", (paid,)).fetchone()[0]
    assert out["bank_mercado"] == "R$ 396,70" and out["coverage_pct"] == 62
    assert out["bank"]["mercado_without_receipt"] == {"count": 1, "total": "R$ 150,00"}
    assert out["bank"]["receipts_not_in_bank"] == {"count": 0, "total": "R$ 0,00"}
    top = out["groups"][0]
    assert top["category"] == "Bebê" and top["change_pct"] == 122.2
    assert top["top"] == ["FRALDA EXEMPLO XG 116 (R$ 199,80)"]

    changes = out["prices"]
    up = {c["product"]: c for c in changes["up"]}
    assert up["FRALDA EXEMPLO XG 116"]["before"] == "R$ 0,775/un"
    assert up["FRALDA EXEMPLO XG 116"]["now"] == "R$ 0,861/un"
    assert up["ARROZ T1 TIO EXEMPLO 5KG"]["pct"] == 12.0
    assert [c["product"] for c in changes["down"]] == ["TOMATE ITALIANO"]
    # The same quantities at the earlier prices: 24.90 + 8.00 + 2 x 89.90 = 212.70.
    assert changes["basket"] == {
        "products": 3,
        "pct": 10.3,
        "now": "R$ 234,70",
        "at_earlier_prices": "R$ 212,70",
    }
    assert "Bebê" in out["summary"] and "62% do gasto em Mercado" in out["summary"]


def test_grocery_spending_without_receipts() -> None:
    out = kitchen().grocery_spending("last_month")
    assert out["receipts"] == 0 and "Nenhuma nota" in out["summary"]
    with pytest.raises(store.KitchenError):
        kitchen().grocery_spending("ontem")


def test_receipts_are_linked_to_the_transaction_that_paid_them(conn: sqlite3.Connection) -> None:
    k = kitchen()
    # A transfer with the same amount never counts; neither does a purchase 6 days later.
    add_tx(conn, "2026-10-02", -136_80, "PIX ENVIADO", excluded="internal_transfer")
    add_tx(conn, "2026-10-08", -136_80, "SUPERMERCADO EXEMPLO", category="Mercado")
    # Two candidates, same amount: the store's name decides, not the closest date.
    other = add_tx(conn, "2026-10-02", -136_80, "POSTO SINTETICO", category="Combustivel")
    right = add_tx(conn, "2026-10-03", -136_80, "EXEMPLO CURITIBA", account="card")
    conn.commit()
    out = k.read_receipt(qr(KEY_1))
    assert out["paid_with"] == {"day": "2026-10-03", "account": "Cartao Sintetico ****0002"}
    assert "Achei a compra no extrato: Cartao Sintetico ****0002, 03/10" in out["summary"]
    (txn,) = conn.execute("SELECT txn_id FROM receipts").fetchone()
    assert txn == right != other

    # The bank's category of the purchase is shown when it isn't 'Mercado'.
    match = k.grocery_spending("2026-10")["bank"]
    assert match["receipts_in_other_categories"] == [
        {"category": "Groceries", "count": 1, "total": "R$ 136,80"}
    ]


def test_a_tie_stays_unlinked_until_it_can_be_told_apart(conn: sqlite3.Connection) -> None:
    k = kitchen()
    a = add_tx(conn, "2026-10-02", -136_80, "COMPRA CARTAO", category="Mercado")
    add_tx(conn, "2026-10-02", -136_80, "COMPRA CARTAO", category="Mercado", account="card")
    conn.commit()
    out = k.read_receipt(qr(KEY_1))
    assert out["paid_with"] is None and "Ainda não achei" in out["summary"]
    summary = k.grocery_spending("2026-10")["summary"]
    assert "1 nota(s) sem compra no extrato" in summary
    # Once one of them is gone (a pending purchase replaced on sync), the other one links.
    conn.execute("DELETE FROM transactions WHERE id != ?", (a,))
    conn.commit()
    assert groceries.link_receipts(conn, TODAY) == 1
    # Deleting the transaction unlinks the receipt instead of failing.
    conn.execute("DELETE FROM transactions WHERE id = ?", (a,))
    conn.commit()
    assert conn.execute("SELECT txn_id FROM receipts").fetchone()[0] is None

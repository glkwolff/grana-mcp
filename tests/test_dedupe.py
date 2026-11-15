"""Golden cases for pipeline/dedupe.py. 100% synthetic: names, amounts, ids and
document hashes are made up; no real-data shapes beyond the documented fields.

Household used by most tests (one profile, two people, like HANDOFF U1):
* person A: Nubank-like checking (item-nu-a) and Itaú-like checking (item-itau-a);
* person B: Nubank-like checking + credit card in the same item (item-nu-b).
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from datetime import date

import pytest

from grana_mcp import config
from grana_mcp.pipeline import dedupe
from grana_mcp.pipeline.dedupe import Context, Row, compute, run_dedupe
from grana_mcp.server import GranaTools
from grana_mcp.store import db

TODAY = date(2026, 11, 15)
_ids = itertools.count(1)

A_CHK = "acc-nu-a"
A_ITAU = "acc-itau-a"
B_CHK = "acc-nu-b"
B_CARD = "acc-nu-b-card"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO profiles VALUES ('casa', 'Casa', 'x');"
        "INSERT INTO people (id, display_name, created_at) VALUES "
        "('pa', 'Pessoa A', 'x'), ('pb', 'Pessoa B', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('item-nu-a', 'casa'), "
        "('item-itau-a', 'casa'), ('item-nu-b', 'casa');"
    )
    c.executemany(
        "INSERT INTO accounts (id, item_id, kind, name, number_masked, owner_person_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (A_CHK, "item-nu-a", "checking", "Conta A", "0001", "pa"),
            (A_ITAU, "item-itau-a", "checking", "Conta Itau A", "0002", "pa"),
            (B_CHK, "item-nu-b", "checking", "Conta B", "0003", "pb"),
            (B_CARD, "item-nu-b", "credit_card", "Cartao B", "0004", "pb"),
        ],
    )
    yield c
    c.close()


def tx(
    conn: sqlite3.Connection,
    account: str,
    posted: str,
    cents: int,
    desc: str,
    *,
    op: str | None = None,
    category: str | None = None,
    doc_hash: str | None = None,
    merchant: str | None = None,
    installment: tuple[int, int] | None = None,
) -> str:
    tx_id = f"t{next(_ids):05d}"
    conn.execute(
        "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
        "description_raw, merchant, provider_category, operation_type, counterpart_doc_hash, "
        "installment_number, installment_total, status, source, synced_at) "
        "VALUES (?, ?, 'casa', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'POSTED', 'pluggy', 'x')",
        (
            tx_id,
            account,
            posted,
            cents,
            desc,
            merchant,
            category,
            op,
            doc_hash,
            installment[0] if installment else None,
            installment[1] if installment else None,
        ),
    )
    return tx_id


def state(conn: sqlite3.Connection, tx_id: str) -> tuple[str | None, str | None, str | None]:
    r = conn.execute(
        "SELECT excluded_reason, excluded_pair_id, review_flag FROM transactions WHERE id = ?",
        (tx_id,),
    ).fetchone()
    return r[0], r[1], r[2]


def reason(conn: sqlite3.Connection, tx_id: str) -> str | None:
    return state(conn, tx_id)[0]


def spending(period: str) -> int:
    out = GranaTools(config.db_path(), lambda: TODAY).spending_summary(period)
    cents: int = out["total"]["cents"]
    return cents


# --- a. card bill payments ---


def test_nubank_bill_payment_both_sides(conn: sqlite3.Connection) -> None:
    pay = tx(conn, B_CHK, "2026-10-08", -987_65, "Pagamento de fatura", op="OUTROS")
    card = tx(conn, B_CARD, "2026-10-08", 987_65, "Pagamento recebido",
              op="PAGAMENTO_FATURA", category="Credit card payment")  # fmt: skip
    buy = tx(conn, B_CARD, "2026-10-01", -987_65, "MERCADO SINTETICO")
    run_dedupe(conn)
    assert state(conn, pay) == ("card_bill_payment", card, None)
    assert state(conn, card) == ("card_bill_payment", pay, None)
    assert reason(conn, buy) is None
    assert spending("2026-10") == 987_65  # the purchases only, once


def test_bill_payment_of_another_persons_synced_card(conn: sqlite3.Connection) -> None:
    # Person A pays person B's card (different item) with a Pix; card credit 2 days later.
    pay = tx(conn, A_CHK, "2026-10-08", -120_00, "Pagamento de fatura",
             op="PIX", category="Credit card payment")  # fmt: skip
    card = tx(conn, B_CARD, "2026-10-10", 120_01, "Pagamento recebido", op="PAGAMENTO_FATURA")
    run_dedupe(conn)
    assert state(conn, pay) == ("card_bill_payment", card, None)  # ±1 cent, ≤3 days
    assert reason(conn, card) == "card_bill_payment"


def test_itau_style_bill_payment_matched_by_closed_bill(conn: sqlite3.Connection) -> None:
    # SYNTHETIC Itaú-like shape (not observed in real data): the checking debit has
    # no Pluggy category, operationType BOLETO and a "PAGTO FATURA" description;
    # the card is in a separate item and reported no payment transaction, only the
    # payment inside its closed bill (GET /bills).
    conn.executescript(
        "INSERT INTO items (id, profile_id) VALUES ('item-itau-card', 'casa');"
        "INSERT INTO accounts (id, item_id, kind, name, owner_person_id) VALUES "
        "('acc-itau-card', 'item-itau-card', 'credit_card', 'Cartao Itau A', 'pa');"
        "INSERT INTO bills (id, account_id, due_date, closing_date, total_cents, synced_at) "
        "VALUES ('bill-1', 'acc-itau-card', '2026-10-15', '2026-10-05', 432_10, 'x');"
        "INSERT INTO bill_payments (bill_id, seq, payment_date, amount_cents) "
        "VALUES ('bill-1', 0, '2026-10-16', 432_10);"
    )
    pay = tx(conn, A_ITAU, "2026-10-14", -432_10, "ITAU PAGTO FATURA CARTAO 0099", op="BOLETO")
    run_dedupe(conn)
    assert state(conn, pay) == ("card_bill_payment", None, None)


def test_bill_payment_for_unsynced_card_stays_as_spending(conn: sqlite3.Connection) -> None:
    pay = tx(conn, A_CHK, "2026-10-08", -456_78, "Pagamento de fatura", op="OUTROS")
    run_dedupe(conn)
    assert state(conn, pay) == (None, None, "unmatched_bill_payment")
    assert spending("2026-10") == 456_78
    out = GranaTools(config.db_path(), lambda: TODAY).spending_summary("2026-10")
    assert out["review"]["unmatched_bill_payments"]["count"] == 1
    assert "não parece estar sincronizado" in out["caveats"][0]


def test_bill_payment_amount_or_date_mismatch_is_not_paired(conn: sqlite3.Connection) -> None:
    far = tx(conn, A_CHK, "2026-10-01", -100_00, "Pagamento de fatura")
    tx(conn, B_CARD, "2026-10-05", 100_00, "Pagamento recebido", op="PAGAMENTO_FATURA")
    off = tx(conn, A_ITAU, "2026-10-05", -100_02, "PAG FATURA")
    run_dedupe(conn)
    assert state(conn, far)[2] == "unmatched_bill_payment"  # 4 days apart
    assert state(conn, off)[2] == "unmatched_bill_payment"  # 2 cents off


# --- b. internal transfers ---


def test_pix_between_people_by_household_document(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO household_docs (doc_hash, person_id, source) VALUES ('h-b', 'pb', 'manual')"
    )
    out = tx(conn, A_CHK, "2026-10-12", -200_00, "Transferência enviada pelo Pix|PESSOA B",
             op="PIX", category="Transfers", doc_hash="h-b")  # fmt: skip
    # The receiving side: counterpart (A) is not registered, but it pairs with `out`.
    inc = tx(conn, B_CHK, "2026-10-12", 200_00, "Transferência Recebida|PESSOA A",
             op="PIX", category="Transfers", doc_hash="h-a")  # fmt: skip
    # Person B's account not synced: still internal by the document alone.
    alone = tx(conn, A_CHK, "2026-10-20", -35_00, "Pix enviado PESSOA B", op="PIX", doc_hash="h-b")
    run_dedupe(conn)
    assert state(conn, out) == ("internal_transfer", inc, None)
    assert state(conn, inc) == ("internal_transfer", out, None)
    assert state(conn, alone) == ("internal_transfer", None, None)
    assert spending("2026-10") == 0


def test_pix_between_people_by_pair_rule(conn: sqlite3.Connection) -> None:
    out = tx(conn, A_CHK, "2026-10-20", -75_00, "Pix enviado PESSOA B",
             op="PIX", category="Transfer - PIX")  # fmt: skip
    inc = tx(conn, B_CHK, "2026-10-21", 75_00, "Pix recebido PESSOA A", op="PIX")
    run_dedupe(conn)
    assert state(conn, out) == ("internal_transfer", inc, None)
    assert state(conn, inc) == ("internal_transfer", out, None)


def test_same_person_transfer_between_two_banks(conn: sqlite3.Connection) -> None:
    out = tx(conn, A_ITAU, "2026-10-15", -1_000_00, "TED ENVIADA MESMA TITULARIDADE",
             op="TED", category="Same person transfer - TED")  # fmt: skip
    inc = tx(conn, A_CHK, "2026-10-16", 1_000_00, "Transferência recebida",
             category="Same person transfer")  # fmt: skip
    run_dedupe(conn)
    assert (reason(conn, out), reason(conn, inc)) == ("internal_transfer", "internal_transfer")


def test_pair_rule_is_greedy_by_closest_date_and_one_to_one(conn: sqlite3.Connection) -> None:
    d1 = tx(conn, A_CHK, "2026-10-10", -50_00, "Pix enviado", op="PIX")
    c_far = tx(conn, B_CHK, "2026-10-09", 50_00, "Pix recebido", op="PIX")
    c_same = tx(conn, B_CHK, "2026-10-10", 50_00, "Pix recebido", op="PIX")
    run_dedupe(conn)
    assert state(conn, d1)[1] == c_same
    assert reason(conn, c_far) is None  # each row pairs at most once


def test_pair_rule_rejections(conn: sqlite3.Connection) -> None:
    # Too far apart.
    a = tx(conn, A_CHK, "2026-10-01", -60_00, "Pix enviado", op="PIX")
    b = tx(conn, B_CHK, "2026-10-03", 60_00, "Pix recebido", op="PIX")
    # Same account.
    c = tx(conn, A_CHK, "2026-10-05", -61_00, "Pix enviado", op="PIX")
    d = tx(conn, A_CHK, "2026-10-05", 61_00, "Pix recebido", op="PIX")
    # Not transfer-like.
    e = tx(conn, A_CHK, "2026-10-06", -62_00, "PADARIA", op="CARTAO", category="Bakeries")
    f = tx(conn, B_CHK, "2026-10-06", 62_00, "Salario", op="FOLHA_PAGAMENTO", category="Salary")
    # Card accounts never pair.
    g = tx(conn, B_CARD, "2026-10-07", -63_00, "Pix no credito", op="PIX")
    h = tx(conn, A_CHK, "2026-10-07", 63_00, "Pix recebido", op="PIX")
    run_dedupe(conn)
    assert all(reason(conn, x) is None for x in (a, b, c, d, e, f, g, h))


def test_pair_rule_vetoed_by_known_holder_documents(conn: sqlite3.Connection) -> None:
    # Holders known from GET /identity; the debit went to a stranger and the
    # credit came from another stranger: same amount and day by coincidence.
    conn.execute(
        "INSERT INTO item_identities VALUES ('item-nu-a', 'h-a', 'x'), ('item-nu-b', 'h-b', 'x')"
    )
    out = tx(conn, A_CHK, "2026-10-12", -90_00, "Pix enviado", op="PIX", doc_hash="h-stranger-1")
    inc = tx(conn, B_CHK, "2026-10-12", 90_00, "Pix recebido", op="PIX", doc_hash="h-stranger-2")
    run_dedupe(conn)
    assert (reason(conn, out), reason(conn, inc)) == (None, None)


# --- c. investments ---


def test_investment_application_and_redemption(conn: sqlite3.Connection) -> None:
    app = tx(conn, B_CHK, "2026-10-02", -500_00, "Aplicação RDB",
             op="RESGATE_APLIC_FINANCEIRA", category="Investments")  # fmt: skip
    red = tx(conn, B_CHK, "2026-10-20", 300_00, "Resgate RDB", op="RESGATE_APLIC_FINANCEIRA")
    fund = tx(conn, A_CHK, "2026-10-03", -100_00, "FUNDO SINTETICO", op="BOLETO",
              category="Mutual funds")  # fmt: skip
    desc_only = tx(conn, A_ITAU, "2026-10-04", -70_00, "APLICACAO CDB SINTETICO")
    income = tx(conn, B_CHK, "2026-10-31", 1_23, "Rendimento", op="RENDIMENTO_APLIC_FINANCEIRA")
    run_dedupe(conn)
    for x in (app, red, fund, desc_only):
        assert reason(conn, x) == "investment"
    assert reason(conn, income) is None  # income, not an application/redemption
    assert spending("2026-10") == 0


# --- installments (U5) ---


def test_installments_count_in_the_month_charged(conn: sqlite3.Connection) -> None:
    for n, when in enumerate(("2026-10-03", "2026-11-03", "2026-12-03"), start=1):
        tx(conn, B_CARD, when, -30_00, f"LOJA SINTETICA PARC {n}/3", installment=(n, 3))
    run_dedupe(conn)
    excluded = conn.execute("SELECT COUNT(*) FROM transactions WHERE excluded_reason IS NOT NULL")
    assert excluded.fetchone()[0] == 0
    assert spending("2026-10") == 30_00
    assert spending("2026-11") == 30_00
    assert spending("2026-12") == 0  # scheduled (future) installment: card_bills only
    assert spending("last_3_months") == 60_00


# --- d. refunds ---


def test_card_refund_pair(conn: sqlite3.Connection) -> None:
    buy = tx(conn, B_CARD, "2026-09-20", -59_90, "LOJA SINTETICA X", merchant="Loja X")
    other = tx(conn, B_CARD, "2026-06-01", -59_90, "LOJA SINTETICA X")  # older: not chosen
    ref = tx(conn, B_CARD, "2026-10-02", 59_90, "LOJA SINTETICA X", op="ESTORNO")
    run_dedupe(conn)
    assert state(conn, ref) == ("refund_pair", buy, None)
    assert state(conn, buy) == ("refund_pair", ref, None)
    assert reason(conn, other) is None
    assert spending("2026-09") == 0


def test_pix_and_nupay_refund_pairs(conn: sqlite3.Connection) -> None:
    pix = tx(conn, A_CHK, "2026-10-05", -80_00, "Transferência enviada pelo Pix|FULANO SINTETICO",
             op="PIX", category="Transfers", doc_hash="h-stranger")  # fmt: skip
    pix_ref = tx(conn, A_CHK, "2026-10-05", 80_00,
                 "Estorno - Transferência enviada pelo Pix|FULANO SINTETICO",
                 op="PIX", doc_hash="h-stranger")  # fmt: skip
    nupay = tx(conn, B_CHK, "2026-10-06", -25_00, "Compra no débito via NuPay - LOJA Y",
               op="OUTROS")  # fmt: skip
    nupay_ref = tx(conn, B_CHK, "2026-10-09", 25_00, "Estorno - Compra no débito via NuPay",
                   op="OUTROS")  # fmt: skip
    run_dedupe(conn)
    assert state(conn, pix_ref) == ("refund_pair", pix, None)
    assert state(conn, nupay_ref) == ("refund_pair", nupay, None)
    assert spending("2026-10") == 0


def test_unmatched_and_partial_refunds_are_flagged(conn: sqlite3.Connection) -> None:
    buy = tx(conn, B_CARD, "2026-10-01", -100_00, "LOJA SINTETICA Z")
    partial = tx(conn, B_CARD, "2026-10-03", 10_00, "LOJA SINTETICA Z", op="ESTORNO")
    other_name = tx(conn, A_CHK, "2026-10-04", 40_00,
                    "Estorno - Transferência enviada pelo Pix|CICLANO SINTETICO")  # fmt: skip
    tx(conn, A_CHK, "2026-10-02", -40_00, "Transferência enviada pelo Pix|BELTRANO SINTETICO",
       op="PIX")  # fmt: skip
    too_old = tx(conn, B_CARD, "2026-10-10", 77_00, "LOJA VELHA", op="ESTORNO")
    tx(conn, B_CARD, "2026-06-01", -77_00, "LOJA VELHA")  # 131 days before
    cashback = tx(conn, B_CARD, "2026-10-05", 3_00, "Cashback", op="CASHBACK")
    run_dedupe(conn)
    assert reason(conn, buy) is None
    for x in (partial, other_name, too_old):
        assert state(conn, x) == (None, None, "unmatched_refund")
    assert state(conn, cashback) == (None, None, None)
    out = GranaTools(config.db_path(), lambda: TODAY).excluded_items("2026-10")
    assert out["review"]["unmatched_refunds"]["count"] == 3
    assert any("estorno" in c for c in out["caveats"])


# --- determinism ---


def test_reprocess_is_idempotent_and_recomputes(conn: sqlite3.Connection) -> None:
    tx(conn, B_CHK, "2026-10-08", -987_65, "Pagamento de fatura", op="OUTROS")
    tx(conn, B_CARD, "2026-10-08", 987_65, "Pagamento recebido", op="PAGAMENTO_FATURA")
    tx(conn, A_CHK, "2026-10-05", -80_00, "Pix enviado|FULANO", op="PIX", doc_hash="h-x")
    tx(conn, A_CHK, "2026-10-05", 80_00, "Estorno - Pix enviado|FULANO", op="PIX")
    tx(conn, B_CARD, "2026-10-01", -10_00, "LOJA", merchant="Loja")
    tx(conn, B_CARD, "2026-10-02", 10_00, "LOJA ESTORNO", op="ESTORNO", merchant="Loja")
    tx(conn, A_CHK, "2026-10-20", -75_00, "Pix enviado", op="PIX")
    tx(conn, B_CHK, "2026-10-21", 75_00, "Pix recebido", op="PIX")
    tx(conn, A_CHK, "2026-10-22", -1_00, "Pagamento de fatura")
    first = run_dedupe(conn)
    assert first.changed == 9
    snapshot = conn.execute(
        "SELECT id, excluded_reason, excluded_pair_id, review_flag FROM transactions ORDER BY id"
    ).fetchall()
    second = run_dedupe(conn)
    assert second.changed == 0
    again = conn.execute(
        "SELECT id, excluded_reason, excluded_pair_id, review_flag FROM transactions ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in again] == [tuple(r) for r in snapshot]
    assert second.by_reason == {
        "card_bill_payment": 2,
        "internal_transfer": 2,
        "investment": 0,
        "refund_pair": 4,
    }
    assert second.flags == {"unmatched_bill_payment": 1, "unmatched_refund": 0}
    # The card-side credit disappears (re-sync): the checking payment is
    # recomputed (still excluded, now through the same-item card, unpaired).
    conn.execute("DELETE FROM transactions WHERE operation_type = 'PAGAMENTO_FATURA'")
    third = run_dedupe(conn)
    assert third.changed == 1
    pay = conn.execute(
        "SELECT excluded_reason, excluded_pair_id FROM transactions "
        "WHERE description_raw = 'Pagamento de fatura'"
    ).fetchone()
    assert tuple(pay) == ("card_bill_payment", None)


def test_compute_is_order_independent() -> None:
    def row(i: str, acc: str, day: int, cents: int) -> Row:
        return Row(
            id=i, account_id=acc, kind="checking", item_id=acc, posted=date(2026, 10, day),
            cents=cents, description="Pix", merchant="", category="", op="PIX", doc_hash=None,
        )  # fmt: skip

    rows = [row("1", "x", 1, -5), row("2", "y", 1, 5), row("3", "y", 2, 5), row("4", "z", 1, -5)]
    first = compute(rows, Context())
    assert compute(list(reversed(rows)), Context()) == first
    assert {k for k, v in first.items() if v.reason} == {"1", "2", "3", "4"}


def test_rule_order_bill_payment_wins_over_transfer(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO household_docs (doc_hash, person_id, source) VALUES ('h-b', 'pb', 'manual')"
    )
    pay = tx(conn, B_CHK, "2026-10-08", -10_00, "Pagamento de fatura", op="PIX", doc_hash="h-b")
    run_dedupe(conn)
    assert reason(conn, pay) == "card_bill_payment"


def test_overrides_hook_is_identity(conn: sqlite3.Connection) -> None:
    decisions = {"x": dedupe.Decision("investment")}
    assert dedupe.apply_overrides(conn, decisions) is decisions

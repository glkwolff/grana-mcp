"""Dedupe: decide which rows are not spending (HANDOFF §7) and why.

Runs at the end of every sync and via ``grana-mcp reprocess`` (no network). It is
deterministic and idempotent: every run recomputes ``excluded_reason``,
``excluded_pair_id`` and ``review_flag`` for ALL rows from the provider columns,
so running it twice gives the same result.

Rules, in order; a row claimed by one rule is not looked at by the next ones:

a. ``card_bill_payment``
   * card side: credits on a credit card with operationType ``PAGAMENTO_FATURA``
     or category "Credit card payment";
   * checking side: debits that pay a card bill (category "Credit card payment",
     operationType ``PAGAMENTO_FATURA`` or a "pagamento de fatura"-like
     description), ONLY when the paid card is plausibly synced: a matching
     card-side payment credit or closed-bill payment (same amount ±1 cent, dates
     within 3 days), or a credit card account in the same item. Otherwise the
     row stays as spending with ``review_flag='unmatched_bill_payment'``.
b. ``internal_transfer``
   * the counterpart document (salted hash) is a household document;
   * pairs between two different non-card accounts: one debit + one credit,
     same absolute amount, dates at most 1 day apart, transfer-like
     (operationType PIX/TED/DOC/TRANSFERENCIA_* or a transfer category). Greedy
     by closest date, ties by ids; each row pairs at most once. A pair is
     rejected when a counterpart document is known and does not belong to the
     holder of the other account (when that holder's documents are known).
c. ``investment``: operationType ``RESGATE_APLIC_FINANCEIRA``, a category of
   Pluggy's "Investments" family, or an "Aplicação/Resgate RDB/CDB…" description
   on a non-card account.
d. ``refund_pair``: a refund credit (card ``ESTORNO`` or a description starting
   with "Estorno") and an earlier debit on the same account with the same
   absolute amount, at most 120 days before, with a similar description or the
   same merchant; both rows are marked. Unmatched or partial refunds stay
   unmarked (credits are never spending) with ``review_flag='unmatched_refund'``.

Never logs amounts or descriptions; only counts.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

from grana_mcp.pipeline.normalize import fold, is_refund_description, similar_descriptions

log = logging.getLogger(__name__)

CARD_BILL_PAYMENT = "card_bill_payment"
INTERNAL_TRANSFER = "internal_transfer"
INVESTMENT = "investment"
REFUND_PAIR = "refund_pair"
REASONS = (CARD_BILL_PAYMENT, INTERNAL_TRANSFER, INVESTMENT, REFUND_PAIR)

UNMATCHED_BILL_PAYMENT = "unmatched_bill_payment"
UNMATCHED_REFUND = "unmatched_refund"

AMOUNT_TOLERANCE_CENTS = 1  # bill payment pairs
BILL_MATCH_DAYS = 3
TRANSFER_MATCH_DAYS = 1
REFUND_WINDOW_DAYS = 120

PAYMENT_OPERATION = "PAGAMENTO_FATURA"
PAYMENT_CATEGORY = "credit card payment"
REFUND_OPERATION = "ESTORNO"
# Documented bank-account operationType values (docs/products/transactions).
TRANSFER_OPERATIONS = frozenset({"PIX", "TED", "DOC", "TRANSFERENCIA_MESMA_INSTITUICAO"})
INVESTMENT_OPERATIONS = frozenset({"RESGATE_APLIC_FINANCEIRA"})
# Pluggy category tree (docs/products/transaction-categorization), casefolded.
# "Proceeds interests and dividends" and "Margin" are left out: income / costs.
INVESTMENT_CATEGORIES = frozenset(
    {
        "investments",
        "automatic investment",
        "fixed income",
        "mutual funds",
        "variable income",
        "pension",
    }
)
TRANSFER_CATEGORIES = frozenset({"transfers", "same person transfer", "third-party transfers"})
TRANSFER_CATEGORY_PREFIXES = ("transfer - ", "same person transfer - ")

_BILL_DESC_RE = re.compile(r"\b(pagamento|pagto|pgto|pag)\.?\s+(d[ae]\s+)?fatura\b")
# Card-side credit as written in bank exports (e.g. Nubank card CSV: "Pagamento recebido").
_CARD_PAYMENT_DESC_RE = re.compile(r"^pagamento recebido\b")
_INVEST_DESC_RE = re.compile(
    r"^(aplicacao|resgate)\b.*\b(rdb|cdb|lci|lca|caixinha|caixinhas|poupanca|tesouro|fundo)\b"
)


@dataclass(frozen=True, slots=True)
class Row:
    id: str
    account_id: str
    kind: str
    item_id: str | None
    posted: date
    cents: int
    description: str
    merchant: str
    category: str  # casefolded, "" when missing
    op: str  # upper case, "" when missing
    doc_hash: str | None

    @property
    def is_card(self) -> bool:
        return self.kind == "credit_card"


@dataclass(frozen=True, slots=True)
class Decision:
    reason: str | None = None
    pair_id: str | None = None
    flag: str | None = None


@dataclass
class Context:
    household: set[str] = field(default_factory=set)
    # account id -> documents (hashes) known to belong to its holder
    holder_docs: dict[str, set[str]] = field(default_factory=dict)
    # items that have a credit card account
    card_items: set[str] = field(default_factory=set)
    # (payment date, positive cents) of closed-bill payments on synced cards
    bill_payments: list[tuple[date, int]] = field(default_factory=list)


@dataclass
class DedupeResult:
    by_reason: dict[str, int]
    flags: dict[str, int]
    changed: int
    total: int


# --- classifiers ---


def is_card_payment_credit(r: Row) -> bool:
    if not r.is_card or r.cents <= 0:
        return False
    return (
        r.op == PAYMENT_OPERATION
        or r.category == PAYMENT_CATEGORY
        or bool(_CARD_PAYMENT_DESC_RE.search(fold(r.description)))
    )


def is_checking_bill_payment(r: Row) -> bool:
    if r.is_card or r.cents >= 0:
        return False
    return (
        r.category == PAYMENT_CATEGORY
        or r.op == PAYMENT_OPERATION
        or bool(_BILL_DESC_RE.search(fold(r.description)))
    )


def is_refund_credit(r: Row) -> bool:
    if r.cents <= 0:
        return False
    return (r.is_card and r.op == REFUND_OPERATION) or is_refund_description(r.description)


def is_transfer_like(r: Row) -> bool:
    if r.op in TRANSFER_OPERATIONS or r.op.startswith("TRANSFERENCIA"):
        return True
    return r.category in TRANSFER_CATEGORIES or r.category.startswith(TRANSFER_CATEGORY_PREFIXES)


def is_investment(r: Row) -> bool:
    if r.op in INVESTMENT_OPERATIONS or r.category in INVESTMENT_CATEGORIES:
        return True
    return not r.is_card and bool(_INVEST_DESC_RE.search(fold(r.description)))


# --- rules ---


def _days(a: date, b: date) -> int:
    return abs((a - b).days)


def _rule_bill_payments(rows: list[Row], ctx: Context, out: dict[str, Decision]) -> None:
    card_credits = [r for r in rows if is_card_payment_credit(r)]
    for r in card_credits:
        out[r.id] = Decision(CARD_BILL_PAYMENT)
    checking = sorted(
        (r for r in rows if r.id not in out and is_checking_bill_payment(r)),
        key=lambda r: (r.posted, r.id),
    )
    # One-to-one pairing with card-side credits (closest date, then ids).
    candidates = sorted(
        (_days(d.posted, c.posted), d.posted, d.id, c.id)
        for d in checking
        for c in card_credits
        if abs(c.cents + d.cents) <= AMOUNT_TOLERANCE_CENTS
        and _days(d.posted, c.posted) <= BILL_MATCH_DAYS
    )
    paired: dict[str, str] = {}
    used: set[str] = set()
    for _, _, d_id, c_id in candidates:
        if d_id in paired or c_id in used:
            continue
        paired[d_id] = c_id
        used.add(c_id)
        out[c_id] = Decision(CARD_BILL_PAYMENT, pair_id=d_id)
    for d in checking:
        if d.id in paired:
            out[d.id] = Decision(CARD_BILL_PAYMENT, pair_id=paired[d.id])
        elif (d.item_id is not None and d.item_id in ctx.card_items) or any(
            abs(cents + d.cents) <= AMOUNT_TOLERANCE_CENTS
            and _days(when, d.posted) <= BILL_MATCH_DAYS
            for when, cents in ctx.bill_payments
        ):
            out[d.id] = Decision(CARD_BILL_PAYMENT)
        else:
            out[d.id] = Decision(flag=UNMATCHED_BILL_PAYMENT)


def _holder_consistent(debit: Row, credit: Row, ctx: Context) -> bool:
    """Reject a pair whose counterpart documents contradict the known holders."""
    credit_holder = ctx.holder_docs.get(credit.account_id)
    if debit.doc_hash and credit_holder and debit.doc_hash not in credit_holder:
        return False
    debit_holder = ctx.holder_docs.get(debit.account_id)
    return not (credit.doc_hash and debit_holder and credit.doc_hash not in debit_holder)


def _rule_internal_transfers(rows: list[Row], ctx: Context, out: dict[str, Decision]) -> None:
    for r in rows:
        if _is_free(r, out) and r.doc_hash is not None and r.doc_hash in ctx.household:
            out[r.id] = Decision(INTERNAL_TRANSFER)

    def pairable(r: Row) -> bool:
        claimed = out.get(r.id)
        free = claimed is None or claimed.reason in (None, INTERNAL_TRANSFER)
        return (
            free
            and not r.is_card
            and r.cents != 0
            and is_transfer_like(r)
            and not is_refund_credit(r)
        )

    credits_by_amount: dict[int, list[Row]] = defaultdict(list)
    debits: list[Row] = []
    for r in rows:
        if not pairable(r):
            continue
        if r.cents > 0:
            credits_by_amount[r.cents].append(r)
        else:
            debits.append(r)
    candidates = sorted(
        (_days(d.posted, c.posted), d.posted, d.id, c.id)
        for d in debits
        for c in credits_by_amount.get(-d.cents, ())
        if c.account_id != d.account_id
        and _days(d.posted, c.posted) <= TRANSFER_MATCH_DAYS
        and _holder_consistent(d, c, ctx)
    )
    used: set[str] = set()
    for _, _, d_id, c_id in candidates:
        if d_id in used or c_id in used:
            continue
        used.update((d_id, c_id))
        out[d_id] = Decision(INTERNAL_TRANSFER, pair_id=c_id)
        out[c_id] = Decision(INTERNAL_TRANSFER, pair_id=d_id)


def _is_free(r: Row, out: Mapping[str, Decision]) -> bool:
    d = out.get(r.id)
    return d is None or d.reason is None


def _rule_investments(rows: list[Row], out: dict[str, Decision]) -> None:
    for r in rows:
        if _is_free(r, out) and is_investment(r):
            out[r.id] = Decision(INVESTMENT)


def _rule_refunds(rows: list[Row], out: dict[str, Decision]) -> None:
    debits_by_key: dict[tuple[str, int], list[Row]] = defaultdict(list)
    for r in rows:
        if r.cents < 0 and _is_free(r, out):
            debits_by_key[(r.account_id, -r.cents)].append(r)
    refunds = sorted(
        (r for r in rows if _is_free(r, out) and is_refund_credit(r)),
        key=lambda r: (r.posted, r.id),
    )
    used: set[str] = set()
    for refund in refunds:
        matches = [
            d
            for d in debits_by_key.get((refund.account_id, refund.cents), ())
            if d.id not in used
            and 0 <= (refund.posted - d.posted).days <= REFUND_WINDOW_DAYS
            and (
                similar_descriptions(refund.description, d.description)
                or (refund.merchant != "" and refund.merchant == d.merchant)
            )
        ]
        if not matches:
            out[refund.id] = Decision(flag=UNMATCHED_REFUND)
            continue
        # Closest earlier debit; ties by id.
        best = min(matches, key=lambda d: ((refund.posted - d.posted).days, d.id))
        used.add(best.id)
        out[refund.id] = Decision(REFUND_PAIR, pair_id=best.id)
        out[best.id] = Decision(REFUND_PAIR, pair_id=refund.id)


def compute(rows: Iterable[Row], ctx: Context) -> dict[str, Decision]:
    """Pure: a decision for every row id (``Decision()`` = counts as usual)."""
    ordered = sorted(rows, key=lambda r: r.id)
    out: dict[str, Decision] = {}
    _rule_bill_payments(ordered, ctx, out)
    _rule_internal_transfers(ordered, ctx, out)
    _rule_investments(ordered, out)
    _rule_refunds(ordered, out)
    return {r.id: out.get(r.id, Decision()) for r in ordered}


def apply_overrides(
    conn: sqlite3.Connection, decisions: dict[str, Decision]
) -> dict[str, Decision]:
    """The single place where user overrides will replace automatic decisions
    (not implemented yet: there is no override table)."""
    return decisions


# --- I/O ---


def load_rows(conn: sqlite3.Connection) -> list[Row]:
    return [
        Row(
            id=r["id"],
            account_id=r["account_id"],
            kind=r["kind"] or "other",
            item_id=r["item_id"],
            posted=date.fromisoformat(r["posted_date"]),
            cents=r["amount_cents"],
            description=r["description_raw"] or "",
            merchant=fold(r["merchant"]),
            category=(r["provider_category"] or "").strip().casefold(),
            op=(r["operation_type"] or "").strip().upper(),
            doc_hash=r["counterpart_doc_hash"],
        )
        for r in conn.execute(
            "SELECT t.id, t.account_id, a.kind, a.item_id, t.posted_date, t.amount_cents, "
            "t.description_raw, t.merchant, t.provider_category, t.operation_type, "
            "t.counterpart_doc_hash FROM transactions t "
            "LEFT JOIN accounts a ON a.id = t.account_id"
        )
    ]


def load_context(conn: sqlite3.Connection) -> Context:
    ctx = Context(household={r[0] for r in conn.execute("SELECT doc_hash FROM household_docs")})
    for r in conn.execute(
        "SELECT a.id, ii.doc_hash FROM accounts a "
        "JOIN item_identities ii ON ii.item_id = a.item_id "
        "UNION SELECT a.id, h.doc_hash FROM accounts a "
        "JOIN household_docs h ON h.person_id = a.owner_person_id"
    ):
        ctx.holder_docs.setdefault(r[0], set()).add(r[1])
    ctx.card_items = {
        r[0]
        for r in conn.execute("SELECT DISTINCT item_id FROM accounts WHERE kind = 'credit_card'")
    }
    ctx.bill_payments = [
        (date.fromisoformat(r[0]), int(r[1]))
        for r in conn.execute(
            "SELECT p.payment_date, p.amount_cents FROM bill_payments p "
            "WHERE p.payment_date IS NOT NULL"
        )
    ]
    return ctx


def run_dedupe(conn: sqlite3.Connection) -> DedupeResult:
    """Recompute and store the decisions for every row; commits.

    ``conn`` must use ``sqlite3.Row`` as row factory (``store.db.connect`` does).
    """
    rows = load_rows(conn)
    decisions = apply_overrides(conn, compute(rows, load_context(conn)))
    current = {
        r["id"]: Decision(r["excluded_reason"], r["excluded_pair_id"], r["review_flag"])
        for r in conn.execute(
            "SELECT id, excluded_reason, excluded_pair_id, review_flag FROM transactions"
        )
    }
    changes = [
        (d.reason, d.pair_id, d.flag, tx_id)
        for tx_id, d in decisions.items()
        if current.get(tx_id) != d
    ]
    conn.executemany(
        "UPDATE transactions SET excluded_reason = ?, excluded_pair_id = ?, review_flag = ? "
        "WHERE id = ?",
        changes,
    )
    conn.commit()
    by_reason = dict.fromkeys(REASONS, 0)
    flags = dict.fromkeys((UNMATCHED_BILL_PAYMENT, UNMATCHED_REFUND), 0)
    for d in decisions.values():
        if d.reason:
            by_reason[d.reason] += 1
        if d.flag:
            flags[d.flag] += 1
    log.info("dedupe: %d rows, %d changed, %s", len(decisions), len(changes), by_reason)
    return DedupeResult(by_reason=by_reason, flags=flags, changed=len(changes), total=len(rows))

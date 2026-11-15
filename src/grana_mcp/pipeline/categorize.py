"""Categorization (Phase 4). Deterministic, no LLM, no network.

Order (first match wins):

1. user rules (``category_rules`` with ``origin='user'``, highest priority first);
2. built-in Brazilian rules (``rules/categories_br.toml``, file order);
3. Pluggy's category mapped to our taxonomy (vague ones such as "Services" are skipped);
4. Pix/transfer to a natural person (CPF) -> "Transferências";
5. "Outros".

Built-in rules come before Pluggy's category on purpose: on real data Pluggy
labels debit-card purchases at supermarkets as "Shopping" and many Pix
payments to companies as "Services". A specific merchant rule is more reliable.

Descriptions are third-party text: they are only matched here, never sent anywhere.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Literal

log = logging.getLogger(__name__)

MatchType = Literal["contains", "regex", "merchant"]
MATCH_TYPES: tuple[MatchType, ...] = ("contains", "regex", "merchant")

FALLBACK = "Outros"
TRANSFERS = "Transferências"
TAXONOMY: tuple[str, ...] = (
    "Mercado",
    "Restaurantes",
    "Delivery",
    "Transporte",
    "Combustível",
    "Moradia",
    "Contas",
    "Saúde",
    "Farmácia",
    "Educação",
    "Lazer",
    "Assinaturas",
    "Compras",
    "Vestuário",
    "Pets",
    "Viagem",
    "Impostos e taxas",
    "Empréstimos",
    TRANSFERS,
    "Receitas",
    FALLBACK,
)

# Pluggy category (English, from GET /categories) -> taxonomy. Missing or None = no opinion.
PLUGGY_MAP: dict[str, str | None] = {
    # food
    "Groceries": "Mercado",
    "Food and drinks": "Restaurantes",
    "Eating out": "Restaurantes",
    "Food delivery": "Delivery",
    # transport
    "Taxi and ride-hailing": "Transporte",
    "Public transportation": "Transporte",
    "Transportation": "Transporte",
    "Bus tickets": "Transporte",
    "Bicycle": "Transporte",
    "Parking": "Transporte",
    "Tolls and in vehicle payment": "Transporte",
    "Car rental": "Transporte",
    "Automotive": "Transporte",
    "Vehicle maintenance": "Transporte",
    "Vehicle insurance": "Transporte",
    "Gas stations": "Combustível",
    # housing and bills
    "Housing": "Moradia",
    "Rent": "Moradia",
    "Home insurance": "Moradia",
    "Houseware": "Compras",
    "Utilities": "Contas",
    "Electricity": "Contas",
    "Water": "Contas",
    "Gas": "Contas",
    "Internet": "Contas",
    "Mobile": "Contas",
    "Telecommunications": "Contas",
    "TV": "Contas",
    # health
    "Healthcare": "Saúde",
    "Hospital clinics and labs": "Saúde",
    "Dentist": "Saúde",
    "Optometry": "Saúde",
    "Health insurance": "Saúde",
    "Wellness": "Saúde",
    "Wellness and fitness": "Saúde",
    "Gyms and fitness centers": "Saúde",
    "Sports practice": "Saúde",
    "Pharmacy": "Farmácia",
    # education
    "Education": "Educação",
    "School": "Educação",
    "University": "Educação",
    "Kindergarten": "Educação",
    "Online Courses": "Educação",
    "Bookstore": "Educação",
    "Office supplies": "Compras",
    # leisure and subscriptions
    "Leisure": "Lazer",
    "Cinema, theater and concerts": "Lazer",
    "Landmarks and museums": "Lazer",
    "Stadiums and arenas": "Lazer",
    "Tickets": "Lazer",
    "Gaming": "Lazer",
    "Gambling": "Lazer",
    "Lottery": "Lazer",
    "Online bet": "Lazer",
    "Music streaming": "Assinaturas",
    "Video streaming": "Assinaturas",
    "Digital services": "Assinaturas",
    # shopping
    "Shopping": "Compras",
    "Online shopping": "Compras",
    "Electronics": "Compras",
    "Kids and toys": "Compras",
    "Sports goods": "Compras",
    "Clothing": "Vestuário",
    "Pet supplies and vet": "Pets",
    # travel
    "Travel": "Viagem",
    "Accomodation": "Viagem",
    "Airport and airlines": "Viagem",
    "Mileage programs": "Viagem",
    # taxes and fees
    "Taxes": "Impostos e taxas",
    "Income taxes": "Impostos e taxas",
    "Urban land and building tax": "Impostos e taxas",
    "Vehicle ownership taxes and fees": "Impostos e taxas",
    "Traffic tickets": "Impostos e taxas",
    "Tax on financial operations": "Impostos e taxas",
    "Taxes on investments": "Impostos e taxas",
    "Bank fees": "Impostos e taxas",
    "Account fees": "Impostos e taxas",
    "Credit card fees": "Impostos e taxas",
    "Wire transfer fees and ATM fees": "Impostos e taxas",
    "Interests charged": "Impostos e taxas",
    "Late payment and overdraft costs": "Impostos e taxas",
    "Legal obligations": "Impostos e taxas",
    "Insurance": "Impostos e taxas",
    "Life insurance": "Impostos e taxas",
    # loans
    "Loans": "Empréstimos",
    "Loans and financing": "Empréstimos",
    "Financing": "Empréstimos",
    "Real estate financing": "Moradia",
    "Vehicle financing": "Transporte",
    "Student loan": "Educação",
    # transfers
    "Transfers": TRANSFERS,
    "Transfer - PIX": TRANSFERS,
    "Transfer - TED": TRANSFERS,
    "Transfer - DOC": TRANSFERS,
    "Transfer - Bank Slip": TRANSFERS,
    "Transfer - Cash": TRANSFERS,
    "Transfer - Check": TRANSFERS,
    "Transfer - Internal": TRANSFERS,
    "Transfer - Foreign Exchange": TRANSFERS,
    "Third party transfers": TRANSFERS,
    "Third party transfer - PIX": TRANSFERS,
    "Third party transfer - TED": TRANSFERS,
    "Third party transfer - DOC": TRANSFERS,
    "Third party transfer - Bank Slip": TRANSFERS,
    "Third party transfer - Debit Card": TRANSFERS,
    "Same person transfer": TRANSFERS,
    "Same person transfer - PIX": TRANSFERS,
    "Same person transfer - TED": TRANSFERS,
    "Same person transfer - CASH": TRANSFERS,
    "Alimony": TRANSFERS,
    "Donations": TRANSFERS,
    # income
    "Income": "Receitas",
    "Salary": "Receitas",
    "Retirement": "Receitas",
    "Government aid": "Receitas",
    "Non-recurring income": "Receitas",
    "Entrepreneurial activities": "Receitas",
    "Proceeds interests and dividends": "Receitas",
    "Cashback": "Receitas",
    # too vague to trust
    "Services": None,
    "Other": None,
}

_TRANSFER_OPS = frozenset({"PIX", "TED", "DOC", "TRANSFERENCIA_MESMA_INSTITUICAO"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
MAX_PATTERN = 120
MIN_CONTAINS = 3


def norm(text: str | None) -> str:
    """Casefold, strip accents, keep letters and digits separated by single spaces."""
    ascii_text = (
        unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    )
    return " ".join(_NON_ALNUM_RE.sub(" ", ascii_text).split())


# --- merchant extraction (display) ---

_PREFIXES = (
    "transferencia enviada pelo pix",
    "transferencia enviada",
    "transferencia recebida",
    "pagamento de pix qr code",
    "pagamento efetuado",
    "compra no debito via nupay",
    "compra no debito",
    "compra debito",
    "pix enviado",
    "pix recebido",
)
_ACQUIRER_RE = re.compile(
    r"^(dl|dm|mp|pg|ebw|ec|ifd|pag|pagseguro|sumup|stone|amazonmktplc|mercadolivre|shopee|"
    r"99food|99app)\s*\*\s*",
    re.IGNORECASE,
)
_INSTALLMENT_RE = re.compile(
    r"(\s*-\s*parcela\s*\d+\s*/\s*\d+|\s+\d{1,2}\s*/\s*\d{1,2})\s*$", re.IGNORECASE
)
_NUPAY_SUFFIX_RE = re.compile(r"\s*-\s*nupay\s*$", re.IGNORECASE)


def clean_merchant(description: str | None) -> str | None:
    """Best-effort merchant name: text after "|" (Nubank), without known prefixes,
    acquirer tags ("Dl *", "Mp *"), installment and "- NuPay" suffixes."""
    if not description:
        return None
    text = description.split("|", 1)[1] if "|" in description else description
    text = " ".join(text.split())
    folded = norm(text)
    if "|" not in description:
        for prefix in _PREFIXES:
            if folded.startswith(prefix + " "):
                # Itaú style: "Pix enviado NOME", "Compra débito LOJA"
                text = _drop_words(text, len(prefix.split()))
                break
    keep = text
    text = _INSTALLMENT_RE.sub("", text)
    text = _NUPAY_SUFFIX_RE.sub("", text)
    acq = _ACQUIRER_RE.match(text)
    if acq and text[acq.end() :].strip():
        text = text[acq.end() :]
    text = " ".join(text.split()).strip(" -*|")
    return text or keep or None


def _drop_words(text: str, n: int) -> str:
    parts = text.split()
    return " ".join(parts[n:]) if len(parts) > n else text


# --- rules ---


@dataclass(frozen=True)
class Rule:
    category: str
    match_type: MatchType
    pattern: str
    regex: re.Pattern[str]
    origin: Literal["builtin", "user"]
    priority: int = 0
    rule_id: int | None = None

    def matches(self, text: str, merchant: str) -> bool:
        if self.match_type == "merchant":
            return merchant == self.pattern
        return self.regex.search(text) is not None


class RuleError(ValueError):
    """Invalid user rule (message is safe to show)."""


def _word_regex(pattern: str) -> re.Pattern[str]:
    prefix = pattern.endswith("*")
    key = norm(pattern.rstrip("*"))
    if not key:
        raise RuleError("padrão vazio depois de normalizar")
    tail = "" if prefix else r"(?![a-z0-9])"
    return re.compile(r"(?<![a-z0-9])" + re.escape(key) + tail)


def make_rule(
    pattern: str,
    category: str,
    match_type: MatchType = "contains",
    *,
    origin: Literal["builtin", "user"] = "user",
    priority: int = 0,
    rule_id: int | None = None,
) -> Rule:
    pattern = (pattern or "").strip()
    category = " ".join((category or "").split())
    if match_type not in MATCH_TYPES:
        raise RuleError(f"match_type deve ser um de {', '.join(MATCH_TYPES)}")
    if not category or len(category) > 40:
        raise RuleError("categoria vazia ou longa demais (máx. 40)")
    if not pattern or len(pattern) > MAX_PATTERN:
        raise RuleError(f"padrão vazio ou longo demais (máx. {MAX_PATTERN})")
    if match_type == "regex":
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise RuleError(f"regex inválida: {exc}") from None
        if regex.search("") is not None:
            raise RuleError("regex casa texto vazio; seria aplicada a tudo")
        return Rule(category, match_type, pattern, regex, origin, priority, rule_id)
    key = norm(pattern.rstrip("*"))
    if match_type == "contains" and len(key.replace(" ", "")) < MIN_CONTAINS:
        raise RuleError(f"padrão curto demais (mín. {MIN_CONTAINS} letras/dígitos)")
    if match_type == "merchant":
        never = re.compile("$^")
        return Rule(category, match_type, norm(pattern), never, origin, priority, rule_id)
    return Rule(category, match_type, pattern, _word_regex(pattern), origin, priority, rule_id)


def load_builtin_rules() -> list[Rule]:
    data = tomllib.loads(
        resources.files("grana_mcp.rules").joinpath("categories_br.toml").read_text("utf-8")
    )
    rules = []
    for block in data.get("rule", []):
        for pattern in block["patterns"]:
            rules.append(make_rule(pattern, block["category"], origin="builtin"))
    return rules


def load_user_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute(
        "SELECT id, pattern, match_type, category, priority FROM category_rules "
        "WHERE origin = 'user' ORDER BY priority DESC, id DESC"
    ).fetchall()
    rules = []
    for r in rows:
        try:
            rules.append(make_rule(r[1], r[3], r[2], origin="user", priority=r[4], rule_id=r[0]))
        except RuleError:
            log.warning("ignoring invalid user rule %s", r[0])
    return rules


# --- categorization ---


@dataclass(frozen=True)
class TxText:
    description: str | None
    provider_merchant: str | None
    provider_category: str | None
    operation_type: str | None
    counterpart_doc_type: str | None


def categorize(tx: TxText, user_rules: Sequence[Rule], builtin_rules: Sequence[Rule]) -> str:
    merchant = clean_merchant(tx.description) or ""
    text = norm(" ".join(x for x in (tx.description, tx.provider_merchant) if x))
    merchant_key = norm(tx.provider_merchant or merchant)
    for rule in user_rules:
        if rule.matches(text, merchant_key):
            return rule.category
    for rule in builtin_rules:
        if rule.matches(text, merchant_key):
            return rule.category
    mapped = PLUGGY_MAP.get(tx.provider_category or "")
    if mapped:
        return mapped
    if tx.counterpart_doc_type == "CPF" and (tx.operation_type or "") in _TRANSFER_OPS:
        return TRANSFERS
    return FALLBACK


@dataclass
class CategorizeResult:
    total: int
    changed: int
    by_category: Counter[str]


def run_categorize(conn: sqlite3.Connection) -> CategorizeResult:
    """Recompute ``category`` and ``description_clean`` for every row; commits."""
    user_rules = load_user_rules(conn)
    builtin = load_builtin_rules()
    rows = conn.execute(
        "SELECT id, description_raw, merchant, provider_category, operation_type, "
        "counterpart_doc_type, category, description_clean FROM transactions"
    ).fetchall()
    updates: list[tuple[str, str | None, str]] = []
    counts: Counter[str] = Counter()
    for r in rows:
        cat = categorize(TxText(r[1], r[2], r[3], r[4], r[5]), user_rules, builtin)
        clean = clean_merchant(r[1])
        counts[cat] += 1
        if (cat, clean) != (r[6], r[7]):
            updates.append((cat, clean, r[0]))
    conn.executemany(
        "UPDATE transactions SET category = ?, description_clean = ? WHERE id = ?", updates
    )
    conn.commit()
    log.info("categorize: %d rows, %d changed", len(rows), len(updates))
    return CategorizeResult(total=len(rows), changed=len(updates), by_category=counts)


# --- user rule management ---


def add_user_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    match_type: MatchType = "contains",
    priority: int = 0,
) -> Rule:
    rule = make_rule(pattern, category, match_type, priority=priority)
    cur = conn.execute(
        "INSERT INTO category_rules (pattern, match_type, category, priority, origin) "
        "VALUES (?, ?, ?, ?, 'user')",
        (pattern.strip(), match_type, rule.category, priority),
    )
    conn.commit()
    return make_rule(pattern, rule.category, match_type, priority=priority, rule_id=cur.lastrowid)


def list_user_rules(conn: sqlite3.Connection) -> list[Rule]:
    return load_user_rules(conn)


def delete_user_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM category_rules WHERE id = ? AND origin = 'user'", (rule_id,))
    conn.commit()
    return cur.rowcount > 0

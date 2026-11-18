"""Groceries from the household's own receipts: what was bought, grouped by kind of product,
and how each product's price moved between purchases.

Every receipt line is kept (``receipt_lines``). A product is its folded receipt text, so
the same product in the same chain is recognised on every receipt. Its category is learned
once: a new product gets a *suggested* category (from a cheap model call or from the chat
model) that the household reviews; a confirmed one is never overwritten by a suggestion.
Product texts are third-party data: they are only matched and shown, never used as
instructions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Protocol

from grana_mcp.analytics import queries
from grana_mcp.kitchen.prices import RECEIPT_PREFIX, fold, parse_size
from grana_mcp.kitchen.store import KitchenError
from grana_mcp.money import format_brl
from grana_mcp.store.db import now_utc

CATEGORIES: dict[str, str] = {
    "carnes": "Carnes, aves e peixes",
    "frios": "Frios e embutidos",
    "laticinios": "Laticínios e ovos",
    "hortifruti": "Hortifrúti",
    "padaria": "Pães e padaria",
    "mercearia": "Mercearia (grãos, massas, óleo, temperos)",
    "matinais": "Café, chá e matinais",
    "doces": "Doces e snacks",
    "congelados": "Congelados e prontos",
    "bebidas": "Bebidas sem álcool",
    "alcool": "Bebidas alcoólicas",
    "limpeza": "Limpeza",
    "higiene": "Higiene e beleza",
    "bebe": "Bebê",
    "pet": "Pet",
    "casa": "Casa e utilidades",
    "outros": "Outros",
}
UNCATEGORIZED = "Sem categoria"
MAX_SET = 60  # products per call
MAX_MATCHES = 30  # products one name can reach
PRICE_LOOKBACK_DAYS = 365
MIN_CHANGE = 0.03  # price changes below 3% are noise
TOP_CHANGES = 10
WEIGHT_UNITS = ("KG", "KGS", "QUILO")
VOLUME_UNITS = ("L", "LT", "LTS", "LITRO")
SIZE_UNITS = {"g": ("kg", 0.001), "kg": ("kg", 1.0), "ml": ("l", 0.001), "l": ("l", 1.0),
              "un": ("un", 1.0), "dz": ("un", 12.0)}  # fmt: skip


class LineLike(Protocol):
    @property
    def descr(self) -> str: ...
    @property
    def qty(self) -> float: ...
    @property
    def unit(self) -> str: ...
    @property
    def unit_cents(self) -> int: ...
    @property
    def total_cents(self) -> int: ...


def product_key(descr: str) -> str:
    return fold(descr)[:80]


def category_key(value: Any) -> str:
    """'bebidas', 'Bebidas sem álcool' or 'hortifruti' -> the key; KitchenError otherwise."""
    text = fold(str(value or ""))
    if not text:
        raise KitchenError("diga a categoria")
    for key, label in CATEGORIES.items():
        if text in (key, fold(label)):
            return key
    for key, label in CATEGORIES.items():  # 'carnes' -> 'Carnes, aves e peixes'
        if fold(label).startswith(text):
            return key
    raise KitchenError(
        f"categoria desconhecida: {str(value)[:40]}. Use uma de: " + ", ".join(CATEGORIES)
    )


def store_lines(
    conn: sqlite3.Connection, key_hash: str, market: str, day: str, lines: Iterable[LineLike]
) -> int:
    n = 0
    for line in lines:
        product = product_key(line.descr)
        if not product:
            continue
        conn.execute(
            "INSERT INTO receipt_lines (receipt, market, day, product, descr, qty, unit, "
            "unit_cents, total_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_hash,
                market,
                day,
                product,
                line.descr,
                line.qty,
                line.unit,
                line.unit_cents,
                max(line.total_cents, 0),
            ),
        )
        n += 1
    return n


# --- package sizes ---------------------------------------------------------------------------


Size = tuple[float, str]  # (amount in base unit, 'kg' | 'l' | 'un')


def known_sizes(conn: sqlite3.Connection) -> dict[str, tuple[float, str, str]]:
    rows = conn.execute("SELECT product, size, unit, status FROM product_sizes").fetchall()
    return {str(r[0]): (float(r[1]), str(r[2]), str(r[3])) for r in rows}


def line_size(
    product: str, descr: str, unit: str, known: dict[str, tuple[float, str, str]]
) -> Size | None:
    """What one unit of a receipt line holds: sold by weight/volume -> 1 kg/l; else what the
    household confirmed, what the text says ('5KG', 'C/116'), or a suggestion, in that order."""
    u = unit.upper()
    if u in WEIGHT_UNITS:
        return 1.0, "kg"
    if u in VOLUME_UNITS:
        return 1.0, "l"
    k = known.get(product)
    if k and k[2] == "confirmed":
        return k[0], k[1]
    parsed = parse_size(descr)
    if parsed:
        return parsed
    return (k[0], k[1]) if k else None


def needs_size(descr: str, unit: str) -> bool:
    u = unit.upper()
    return u not in WEIGHT_UNITS and u not in VOLUME_UNITS and parse_size(descr) is None


def base_price(unit_cents: int, size: Size | None) -> str | None:
    """'R$ 0,77/un', 'R$ 4,98/kg' (None when the content is unknown)."""
    if not size:
        return None
    amount, base = size
    per = unit_cents / amount
    if base == "un" and per < 100:
        return f"R$ {per / 100:.3f}".replace(".", ",") + "/un"
    return f"{format_brl(round(per))}/{base}"


def fmt_size(size: Size) -> str:
    amount, base = size
    if base == "un":
        return f"{amount:g} un"
    if amount < 1:
        return f"{amount * 1000:g} {'g' if base == 'kg' else 'ml'}"
    return f"{amount:g} {base}"


def set_sizes(
    conn: sqlite3.Connection,
    items: Iterable[tuple[str, Any, Any]],
    status: str,
    by: str | None,
) -> dict[str, Any]:
    """Save (product name, amount, unit g|kg|ml|l|un|dz) of one package. A suggestion
    never replaces a confirmed size. Also fixes the receipt prices the shopping list uses."""
    if status not in ("suggested", "confirmed"):
        raise ValueError("status")
    triples = list(items)
    if not triples:
        raise KitchenError("mande ao menos um produto")
    if len(triples) > MAX_SET:
        raise KitchenError(f"no máximo {MAX_SET} produtos por vez")
    changed: list[dict[str, str]] = []
    missing: list[str] = []
    too_broad: list[str] = []
    now = now_utc()
    for text, amount, unit in triples:
        conv = SIZE_UNITS.get(fold(str(unit or "")))
        try:
            value = float(amount)
        except (TypeError, ValueError):
            value = 0.0
        if not conv or not 0 < value < 100_000:
            raise KitchenError(
                "tamanho inválido: use quantidade > 0 e unidade g, kg, ml, l, un ou dz"
            )
        size, base = value * conv[1], conv[0]
        found = resolve(conn, str(text))
        if not found:
            missing.append(str(text)[:40])
            continue
        if len(found) > MAX_MATCHES:
            too_broad.append(str(text)[:40])
            continue
        for product in found:
            cur = conn.execute(
                "INSERT INTO product_sizes (product, size, unit, status, by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (product) DO UPDATE SET "
                "size = excluded.size, unit = excluded.unit, status = excluded.status, "
                "by = excluded.by, updated_at = excluded.updated_at "
                "WHERE product_sizes.status = 'suggested' OR excluded.status = 'confirmed'",
                (product, size, base, status, by, now),
            )
            if not cur.rowcount:
                continue
            descrs = [
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT descr FROM receipt_lines WHERE product = ? "
                    "AND upper(unit) NOT IN ('KG', 'KGS', 'QUILO', 'L', 'LT', 'LTS', 'LITRO')",
                    (product,),
                )
            ]
            for d in descrs:
                if status == "confirmed" or parse_size(d) is None:
                    conn.execute(
                        "UPDATE price_obs SET size = ?, unit = ? WHERE descr = ? AND market LIKE ?",
                        (size, base, d, RECEIPT_PREFIX + "%"),
                    )
            changed.append({"product": _descr(conn, product), "size": fmt_size((size, base))})
    conn.commit()
    return {"changed": changed, "not_found": missing, "too_broad": too_broad}


def receipt_lines_priced(conn: sqlite3.Connection, key_hash: str) -> list[dict[str, Any]]:
    """One receipt's lines (the same product summed), with the price per unit/kg/l."""
    known = known_sizes(conn)
    rows = conn.execute(
        "SELECT product, descr, unit, unit_cents, SUM(qty), SUM(total_cents) FROM receipt_lines "
        "WHERE receipt = ? GROUP BY product, unit, unit_cents ORDER BY MIN(id)",
        (key_hash,),
    ).fetchall()
    out = []
    for product, descr, unit, unit_cents, qty, total in rows:
        size = line_size(str(product), str(descr), str(unit), known)
        out.append(
            {
                "what": str(descr),
                "qty": round(float(qty), 3),
                "unit": str(unit),
                "price": format_brl(int(unit_cents)),
                "total": format_brl(int(total)),
                "content": fmt_size(size)
                if size and str(unit).upper() not in WEIGHT_UNITS + VOLUME_UNITS
                else None,
                "per": base_price(int(unit_cents), size),
            }
        )
    return out


def uncategorized(
    conn: sqlite3.Connection, receipt: str | None = None, limit: int = MAX_SET
) -> list[dict[str, Any]]:
    """Products with no category yet (of one receipt, or all), most recent first."""
    rows = conn.execute(
        "SELECT l.product, MAX(l.descr), MAX(l.unit), MAX(l.day) AS last FROM receipt_lines l "
        "LEFT JOIN product_categories c ON c.product = l.product "
        "WHERE c.product IS NULL AND (? IS NULL OR l.receipt = ?) "
        "GROUP BY l.product ORDER BY last DESC, MIN(l.id) LIMIT ?",
        (receipt, receipt, limit),
    ).fetchall()
    known = known_sizes(conn)
    return [
        {
            "product": str(r[0]),
            "descr": str(r[1]),
            "needs_size": str(r[0]) not in known and needs_size(str(r[1]), str(r[2])),
        }
        for r in rows
    ]


def resolve(conn: sqlite3.Connection, text: str) -> list[str]:
    """Known products a name points to: the exact receipt text, else every product whose
    text has those words in sequence ('coca' -> 'coca cola 2l', 'coca cola zero lata')."""
    q = product_key(text)
    if not q:
        return []
    exact = conn.execute("SELECT 1 FROM receipt_lines WHERE product = ? LIMIT 1", (q,)).fetchone()
    if exact:
        return [q]
    rows = conn.execute(
        "SELECT DISTINCT product FROM receipt_lines WHERE ' ' || product || ' ' LIKE ? "
        "ORDER BY product LIMIT ?",
        (f"% {q} %", MAX_MATCHES + 1),
    ).fetchall()
    return [str(r[0]) for r in rows]


def set_categories(
    conn: sqlite3.Connection,
    items: Iterable[tuple[str, Any]],
    status: str,
    by: str | None,
) -> dict[str, Any]:
    """Save (product name, category) pairs. A suggestion never replaces a confirmed one."""
    if status not in ("suggested", "confirmed"):
        raise ValueError("status")
    pairs = list(items)
    if not pairs:
        raise KitchenError("mande ao menos um produto")
    if len(pairs) > MAX_SET:
        raise KitchenError(f"no máximo {MAX_SET} produtos por vez")
    changed: list[dict[str, str]] = []
    missing: list[str] = []
    too_broad: list[str] = []
    now = now_utc()
    for text, cat in pairs:
        key = category_key(cat)
        found = resolve(conn, str(text))
        if not found:
            missing.append(str(text)[:40])
            continue
        if len(found) > MAX_MATCHES:
            too_broad.append(str(text)[:40])
            continue
        for product in found:
            cur = conn.execute(
                "INSERT INTO product_categories (product, category, status, by, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (product) DO UPDATE SET "
                "category = excluded.category, status = excluded.status, by = excluded.by, "
                "updated_at = excluded.updated_at "
                "WHERE product_categories.status = 'suggested' OR excluded.status = 'confirmed'",
                (product, key, status, by, now),
            )
            if cur.rowcount:
                changed.append({"product": _descr(conn, product), "category": CATEGORIES[key]})
    conn.commit()
    return {"changed": changed, "not_found": missing, "too_broad": too_broad}


def confirm_suggested(conn: sqlite3.Connection, by: str | None) -> tuple[int, int]:
    """Everything suggested becomes the household's: (categories, sizes) confirmed."""
    now = now_utc()
    n = []
    for table in ("product_categories", "product_sizes"):
        cur = conn.execute(
            f"UPDATE {table} SET status = 'confirmed', by = ?, updated_at = ? "  # noqa: S608
            "WHERE status = 'suggested'",
            (by, now),
        )
        n.append(cur.rowcount)
    conn.commit()
    return n[0], n[1]


def _descr(conn: sqlite3.Connection, product: str) -> str:
    row = conn.execute(
        "SELECT descr FROM receipt_lines WHERE product = ? ORDER BY day DESC LIMIT 1", (product,)
    ).fetchone()
    return str(row[0]) if row else product


def review(conn: sqlite3.Connection, limit: int = MAX_SET) -> dict[str, Any]:
    """What still needs the household: suggested categories and products with none."""
    rows = conn.execute(
        "SELECT c.product, c.category, MAX(l.descr), MAX(l.day) AS last "
        "FROM product_categories c JOIN receipt_lines l ON l.product = c.product "
        "WHERE c.status = 'suggested' GROUP BY c.product ORDER BY c.category, last DESC LIMIT ?",
        (limit,),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(CATEGORIES.get(str(r[1]), str(r[1])), []).append(str(r[2]))
    (n_suggested,) = conn.execute(
        "SELECT COUNT(*) FROM product_categories WHERE status = 'suggested'"
    ).fetchone()
    missing = uncategorized(conn, limit=limit)
    sizes = conn.execute(
        "SELECT s.product, s.size, s.unit, MAX(l.descr) FROM product_sizes s "
        "JOIN receipt_lines l ON l.product = s.product WHERE s.status = 'suggested' "
        "GROUP BY s.product ORDER BY s.product LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "suggested": grouped,
        "suggested_count": int(n_suggested),
        "suggested_sizes": [
            {"product": str(r[3]), "content": fmt_size((float(r[1]), str(r[2])))} for r in sizes
        ],
        "uncategorized": [m["descr"] for m in missing],
        "categories": CATEGORIES,
    }


def receipt_groups(conn: sqlite3.Connection, key_hash: str) -> list[dict[str, Any]]:
    """One receipt's total per category (biggest first), with its products."""
    rows = conn.execute(
        "SELECT c.category, c.status, l.descr, l.total_cents FROM receipt_lines l "
        "LEFT JOIN product_categories c ON c.product = l.product WHERE l.receipt = ? "
        "ORDER BY l.id",
        (key_hash,),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for cat, status, descr, cents in rows:
        label = CATEGORIES.get(str(cat), UNCATEGORIZED) if cat else UNCATEGORIZED
        g = groups.setdefault(label, {"category": label, "cents": 0, "items": [], "new": []})
        g["cents"] += int(cents)
        g["items"].append(str(descr))
        if status == "suggested":
            g["new"].append(str(descr))
    out = sorted(groups.values(), key=lambda g: -g["cents"])
    for g in out:
        g["total"] = format_brl(g["cents"])
    return out


# --- receipts <-> bank transactions -------------------------------------------------------

LINK_DAYS_BEFORE = 1  # the bank may date a purchase the day before (time zone, cut-off)
LINK_DAYS_AFTER = 4  # debit and Pix can post a few days later
LINK_LOOKBACK_DAYS = 120
_NAME_NOISE = frozenset(
    [
        "supermercado",
        "supermercados",
        "mercado",
        "mercados",
        "super",
        "ltda",
        "me",
        "epp",
        "eireli",
        "sa",
        "s",
        "a",
        "cia",
        "comercio",
        "comercial",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "e",
        "alimentos",
        "atacadista",
        "atacado",
        "distribuidora",
        "varejo",
        "filial",
        "loja",
        "lojas",
        "center",
    ]
)


def _name_words(text: str) -> set[str]:
    return {w for w in fold(text).split() if len(w) >= 3 and w not in _NAME_NOISE}


def _link_score(r: sqlite3.Row, ident: str, words: set[str], day: date) -> tuple[int, int, int]:
    """Lower is better: same CNPJ, then a word of the store's name, then the closest date."""
    same_doc = bool(r[2]) and str(r[2])[: len(ident)] == ident
    same_name = bool(words & _name_words(str(r[3])))
    gap = abs((date.fromisoformat(str(r[1])) - day).days)
    return (0 if same_doc else 1, 0 if same_name else 1, gap)


def link_receipts(conn: sqlite3.Connection, today: date) -> int:
    """Find the transaction that paid each receipt not linked yet: the exact amount, from a
    day before to a few days after the purchase, not excluded (bill payment, transfer) and
    not linked to another receipt. With several candidates, the store's CNPJ, then words of
    its name, then the closest date decide; a tie stays unlinked. Returns links made."""
    since = (today - timedelta(days=LINK_LOOKBACK_DAYS)).isoformat()
    receipts = conn.execute(
        "SELECT key_hash, market, day, total_cents, store FROM receipts "
        "WHERE txn_id IS NULL AND total_cents > 0 AND day >= ?",
        (since,),
    ).fetchall()
    made = 0
    for key_hash, market, day, total, store_name in receipts:
        d = date.fromisoformat(str(day))
        rows = conn.execute(
            "SELECT t.id, t.posted_date, t.counterpart_doc_hash, "
            "COALESCE(t.description_raw, '') || ' ' || COALESCE(t.merchant, '') "
            "FROM transactions t WHERE t.amount_cents = ? AND t.posted_date BETWEEN ? AND ? "
            "AND t.excluded_reason IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.txn_id = t.id)",
            (
                -int(total),
                (d - timedelta(days=LINK_DAYS_BEFORE)).isoformat(),
                (d + timedelta(days=LINK_DAYS_AFTER)).isoformat(),
            ),
        ).fetchall()
        if not rows:
            continue
        ident = str(market).removeprefix(RECEIPT_PREFIX)
        words = _name_words(str(store_name))
        ranked = sorted(((_link_score(r, ident, words, d), r) for r in rows), key=lambda x: x[0])
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            continue  # can't tell which one: better unlinked than wrong
        conn.execute(
            "UPDATE receipts SET txn_id = ? WHERE key_hash = ?", (ranked[0][1][0], key_hash)
        )
        made += 1
    if made:
        conn.commit()
    return made


def receipt_payment(conn: sqlite3.Connection, key_hash: str) -> dict[str, str] | None:
    row = conn.execute(
        "SELECT t.posted_date, a.name, a.number_masked, a.kind FROM receipts r "
        "JOIN transactions t ON t.id = r.txn_id LEFT JOIN accounts a ON a.id = t.account_id "
        "WHERE r.key_hash = ?",
        (key_hash,),
    ).fetchone()
    if not row:
        return None
    label = " ".join(str(x) for x in (row[1], row[2]) if x) or "conta"
    return {"day": str(row[0]), "account": label[:60]}


def bank_match(conn: sqlite3.Connection, start: date, end: date, today: date) -> dict[str, Any]:
    """How the period's receipts and the bank's grocery spending line up."""
    a, b = start.isoformat(), min(end, today).isoformat()
    cat = queries.category_sql("t")
    linked = conn.execute(
        f"SELECT {cat}, COUNT(*), -SUM(t.amount_cents) FROM receipts r "  # noqa: S608
        "JOIN transactions t ON t.id = r.txn_id WHERE r.day BETWEEN ? AND ? GROUP BY 1",
        (a, b),
    ).fetchall()
    unpaid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_cents), 0) FROM receipts r WHERE r.txn_id IS NULL "
        "AND r.day BETWEEN ? AND ? AND EXISTS "
        "(SELECT 1 FROM receipt_lines l WHERE l.receipt = r.key_hash)",
        (a, b),
    ).fetchone()
    missing = conn.execute(
        f"SELECT COUNT(*), COALESCE(-SUM(t.amount_cents), 0) FROM transactions t "  # noqa: S608
        f"WHERE {cat} = 'Mercado' AND t.amount_cents < 0 AND t.excluded_reason IS NULL "
        "AND t.posted_date BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.txn_id = t.id)",
        (a, b),
    ).fetchone()
    by_cat = {str(r[0]): (int(r[1]), int(r[2])) for r in linked}
    in_market = by_cat.pop("Mercado", (0, 0))
    return {
        "mercado_with_receipt": {"count": in_market[0], "total": format_brl(in_market[1])},
        "mercado_without_receipt": {"count": int(missing[0]), "total": format_brl(int(missing[1]))},
        "receipts_not_in_bank": {"count": int(unpaid[0]), "total": format_brl(int(unpaid[1]))},
        "receipts_in_other_categories": [
            {"category": c, "count": n, "total": format_brl(v)} for c, (n, v) in by_cat.items()
        ],
        "_with_cents": in_market[1],
        "_without_cents": int(missing[1]),
    }


# --- analysis ------------------------------------------------------------------------------


def _by_category(conn: sqlite3.Connection, start: date, end: date) -> dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(c.category, ''), SUM(l.total_cents) FROM receipt_lines l "
        "LEFT JOIN product_categories c ON c.product = l.product "
        "WHERE l.day BETWEEN ? AND ? GROUP BY 1",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {str(r[0]): int(r[1] or 0) for r in rows}


def _pct(a: int, b: int) -> float | None:
    return round((a - b) / b * 100, 1) if b else None


def _base(size: Size | None) -> str | None:
    return size[1] if size else None


def price_changes(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    """For each product bought in the period: its last price there vs. the purchase before
    (up to a year back, same unit). Plus the household basket: what the period's quantities
    would have cost at the earlier prices."""
    rows = conn.execute(
        "SELECT l.product, l.descr, l.day, l.unit, l.unit_cents, l.qty, m.name "
        "FROM receipt_lines l JOIN markets m ON m.code = l.market "
        "WHERE l.day BETWEEN ? AND ? ORDER BY l.product, l.day, l.id",
        ((start - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat(), end.isoformat()),
    ).fetchall()
    history: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        history.setdefault(str(r[0]), []).append(r)
    known = known_sizes(conn)

    def per_base(r: sqlite3.Row) -> tuple[float, str, Size | None]:
        """(price to compare, how to show it, content)"""
        size = line_size(str(r[0]), str(r[1]), str(r[3]), known)
        if size:
            return int(r[4]) / size[0], base_price(int(r[4]), size) or "", size
        return float(r[4]), format_brl(int(r[4])), None

    changes: list[dict[str, Any]] = []
    now_cost = before_cost = 0.0
    compared = 0
    for obs in history.values():
        in_period = [r for r in obs if r[2] >= start.isoformat()]
        if not in_period:
            continue
        last = in_period[-1]
        now_p, now_txt, now_size = per_base(last)
        base = now_size[1] if now_size else None
        earlier = [
            r for r in obs if r[2] < last[2] and r[3] == last[3] and _base(per_base(r)[2]) == base
        ]
        if not earlier:
            continue
        prev = earlier[-1]
        prev_p, prev_txt, _ = per_base(prev)
        qty = sum(float(r[5]) for r in in_period if r[3] == last[3])
        now_cost += qty * int(last[4])
        before_cost += qty * int(last[4]) * prev_p / now_p
        compared += 1
        change = (now_p - prev_p) / prev_p
        if abs(change) < MIN_CHANGE:
            continue
        changes.append(
            {
                "product": str(last[1]),
                "before": prev_txt,
                "before_day": str(prev[2]),
                "now": now_txt,
                "now_day": str(last[2]),
                "pct": round(change * 100, 1),
                "market": str(last[6]) if last[6] == prev[6] else f"{prev[6]} → {last[6]}",
            }
        )
    up = sorted((c for c in changes if c["pct"] > 0), key=lambda c: -c["pct"])[:TOP_CHANGES]
    down = sorted((c for c in changes if c["pct"] < 0), key=lambda c: c["pct"])[:TOP_CHANGES]
    basket = None
    if compared and before_cost:
        basket = {
            "products": compared,
            "pct": round((now_cost / before_cost - 1) * 100, 1),
            "now": format_brl(round(now_cost)),
            "at_earlier_prices": format_brl(round(before_cost)),
        }
    return {"up": up, "down": down, "basket": basket}


def analysis(
    conn: sqlite3.Connection, start: date, end: date, prev_start: date, prev_end: date
) -> dict[str, Any]:
    cur = _by_category(conn, start, end)
    prev = _by_category(conn, prev_start, prev_end)
    total = sum(cur.values())
    top_rows = conn.execute(
        "SELECT COALESCE(c.category, ''), MAX(l.descr), SUM(l.total_cents) AS cents, "
        "COUNT(*) FROM receipt_lines l LEFT JOIN product_categories c ON c.product = l.product "
        "WHERE l.day BETWEEN ? AND ? GROUP BY 1, l.product ORDER BY cents DESC",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    tops: dict[str, list[str]] = {}
    for cat, descr, cents, times in top_rows:
        bucket = tops.setdefault(str(cat), [])
        if len(bucket) < 3:
            bucket.append(f"{descr} ({format_brl(int(cents))}{f', {times}x' if times > 1 else ''})")
    groups = []
    for key in sorted(cur, key=lambda k: -cur[k]):
        groups.append(
            {
                "category": CATEGORIES.get(key, UNCATEGORIZED),
                "total": format_brl(cur[key]),
                "cents": cur[key],
                "share_pct": round(cur[key] / total * 100, 1) if total else 0.0,
                "previous": format_brl(prev.get(key, 0)),
                "change_pct": _pct(cur[key], prev.get(key, 0)),
                "top": tops.get(key, []),
            }
        )
    receipts = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT r.market) FROM receipts r "
        "WHERE r.day BETWEEN ? AND ? AND EXISTS "
        "(SELECT 1 FROM receipt_lines l WHERE l.receipt = r.key_hash)",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    (pending,) = conn.execute(
        "SELECT COUNT(DISTINCT l.product) FROM receipt_lines l JOIN product_categories c "
        "ON c.product = l.product WHERE c.status = 'suggested' AND l.day BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return {
        "receipts": int(receipts[0]),
        "markets": int(receipts[1]),
        "total": format_brl(total),
        "total_cents": total,
        "previous_total": format_brl(sum(prev.values())),
        "groups": groups,
        "uncategorized_cents": cur.get("", 0),
        "pending_review": int(pending),
        "prices": price_changes(conn, start, end),
    }

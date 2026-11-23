"""Grocery prices from NFC-e based sources, and where to buy the shopping list.

Source today: Menor Preço (Nota Paraná, PR), the public price search of the
Paraná state treasury. Its JSON service is NOT documented; it may change, so it
is isolated here, cached for 24 h per search term, rate-limited and never on the
critical path (no prices -> the list still works, just without "where").

What is sent to the source: the search term and the household's location (geohash,
at the same precision the official site uses) and radius. Nothing else.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from types import EllipsisType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from grana_mcp.kitchen.store import Need
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

log = logging.getLogger(__name__)

BASE_URL = "https://menorpreco.notaparana.pr.gov.br/api/v1"
USER_AGENT = "grana-mcp (uso pessoal; lista de compras da casa)"
CACHE_HOURS = 24
# Prices only count on the day they were looked up (the household's call: an old price is
# worse than none). Receipt prices: fetched today, from sales of the last few days (sales
# made today alone would leave the morning empty). Noted prices: noted today.
FRESH_DAYS = 7
NOTED_DAYS = 0
# Menor Preço's anti-bot answer: scrambled store names, the bare search word as the product
# ("ARROZ"), fake prices and ~85-char random store ids. Real answers have full receipt
# descriptions and ids of 20-45 chars (the length varies, so it alone is not enough).
POISONED_CODE_LEN = 70
LOCAL_PREFIXES = ("osm:", "manual:")  # markets not from the receipt source
RECEIPT_PREFIX = "nfce:"  # stores from the household's own receipts (kitchen/receipt.py)
BY_WEIGHT = ("por kg", "por l")  # suffix of a noted price that is per kg/l, any amount
CONNECT_TIMEOUT_S = 3.0
CONNECT_TRIES = 4
PAGES = 2  # the official site asks for one page of 50; keep a second one for short pages
PAGE_SIZE = 50
DATA_WEEK = 2  # the source's "última semana"; -1 means "últimos 2 meses" (mostly discarded)
MIN_INTERVAL_S = 1.0
PAIR_MIN_SAVING = 15_00  # a second market must save at least R$ 15 ...
PAIR_MIN_SHARE = 0.05  # ... and 5% of the basket

_SIZE_RE = re.compile(
    r"(?:(\d+)\s*X\s*)?(\d+(?:[.,]\d+)?)\s*(KG|KGS|G|GR|GRS|L|LT|LTS|ML|UN|UND|UNID|UNIDADES)\b"
)
_DOZEN_RE = re.compile(r"\b(DZ|DUZIA)\b")
_BOX_RE = re.compile(r"\b(?:C|CX|C/)\s?(\d{1,3})\b")  # "OVOS BCO C10": box of 10
_LOOSE_RE = re.compile(r"(?<![\d.])\s*\b(KG|KILO)\b")  # "TOMATE KG": priced per kg
_EACH_RE = re.compile(r"\b(UN|UND|UNID|UNIDADE)\b")  # "BATATA LAVADA UN": priced per unit
# Processed versions of a fresh item ("CEBOLA EM PO", "ALHO DESIDRATADO", "ALHO PORO",
# "CALDO ... LEGUMES"): left out unless the search asks for them.
_PROCESSED = ("DESID", "FLOCO", "CALDO", "TEMPER", "COND", "SAZON", "PORO", "SOPINHA",
              "GRANUL", "REFRIG", "REFRESC", "SALGAD", "CHIPS")  # fmt: skip
# Fresh produce: a receipt line without a size ("CEBOLA") is the price per kg.
PRODUCE = {"cebola", "alho", "tomate", "batata", "cenoura", "beterraba", "abobrinha", "chuchu",
           "pimentao", "pepino", "mandioca", "aipim", "inhame", "repolho", "abobora", "banana",
           "maca", "laranja", "mamao", "manga", "abacate", "melancia", "melao", "pera", "uva",
           "gengibre", "limao", "kiwi", "maracuja", "berinjela", "vagem", "quiabo"}  # fmt: skip
OUTLIER_FACTOR = 3.0  # an offer costing 3x the median per kg/l/un is a different product
_PLACE_STOP = {"da", "do", "de", "das", "dos", "e", "supermercado", "supermercados", "mercado",
               "mercados", "ltda", "sa", "me", "eireli"}  # fmt: skip


class Fetcher(Protocol):
    def __call__(self, term: str, geohash: str, radius_km: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class Offer:
    market: str
    name: str
    descr: str
    cents: int
    size: float
    unit: str
    sold_at: str
    loose: bool = False
    # converted with the household's equivalence: (kg or l in one unit, "kg" or "l")
    per_un: tuple[float, str] | None = None

    @property
    def per_unit(self) -> float:
        return self.cents / self.size

    @property
    def per_base(self) -> float:
        """Price per kg/l of a converted offer (what the store actually charges by)."""
        if self.per_un and self.unit == "un":
            return self.per_unit / self.per_un[0]
        return self.per_unit


Equiv = dict[str, float]  # {"kg": kg in one unit, "l": litres in one unit}


def equivalence(conn: sqlite3.Connection, term: str) -> Equiv:
    """How much one unit of ``term`` weighs (or holds), as the household/assistant set it
    (item_units); falls back to the first word ('limao taiti' -> 'limao')."""
    for key in dict.fromkeys([term, term.split()[0] if term else ""]):
        row = conn.execute(
            "SELECT kg_per_un, l_per_un FROM item_units WHERE search = ?", (key,)
        ).fetchone()
        if row:
            return {u: float(v) for u, v in zip(("kg", "l"), row, strict=True) if v}
    return {}


def set_equivalence(
    conn: sqlite3.Connection, term: str, kg: float | None, litres: float | None, by: str | None
) -> None:
    conn.execute(
        "INSERT INTO item_units (search, kg_per_un, l_per_un, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (search) DO UPDATE SET "
        "kg_per_un = COALESCE(excluded.kg_per_un, kg_per_un), "
        "l_per_un = COALESCE(excluded.l_per_un, l_per_un), "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (term, kg, litres, now_utc(), by),
    )
    conn.commit()


def _convert(
    size: float, u: str, unit: str, eq: Equiv
) -> tuple[float, str, tuple[float, str] | None]:
    """A size in ``u`` expressed in the wanted ``unit`` (kg/l <-> un by the equivalence)."""
    if unit == "un" and eq.get(u):
        return size / eq[u], "un", (eq[u], u)
    if u == "un" and eq.get(unit):
        return size * eq[unit], unit, (eq[unit], unit)
    return size, u, None


def parse_size(descr: str) -> tuple[float, str] | None:
    """Package size in base units from a receipt description ('ARROZ T1 5KG' -> 5 kg)."""
    text = descr.upper().replace(",", ".")
    found = list(_SIZE_RE.finditer(text))
    m = found[-1] if found else None  # last match: sizes usually come at the end
    if m:
        packs = int(m.group(1)) if m.group(1) else 1
        value = float(m.group(2)) * packs
        unit = m.group(3)
        if unit in ("KG", "KGS"):
            return value, "kg"
        if unit in ("G", "GR", "GRS"):
            return value / 1000, "kg"
        if unit in ("L", "LT", "LTS"):
            return value, "l"
        if unit == "ML":
            return value / 1000, "l"
        return value, "un"
    if _DOZEN_RE.search(text):
        return 12.0, "un"
    if box := _BOX_RE.search(text):
        return float(box.group(1)), "un"
    if _LOOSE_RE.search(text):
        return 1.0, "kg"
    return None


def relevant(descr: str, term: str) -> bool:
    """Receipts start with the product noun: 'ARROZ BURITI 5KG' is rice, 'DOG CHOW ... ARROZ'
    is not. The first word must share a prefix with the first search word, and processed
    versions ('CEBOLA EM PO', 'ALHO PORO') are out unless the search asks for them."""
    words = term.split()
    found = [w for w in re.split(r"[^A-Z]+", fold(descr).upper()) if w]  # 'LIMÃO' -> 'LIMAO'
    if not words or not found:
        return False
    key = words[0].upper()
    prefix = key[:4] if len(key) > 4 else key[:3]
    if not found[0].startswith(prefix):
        return False
    asked = term.upper().split()
    for w in found[1:]:
        hit = "PO" if w == "PO" else next((p for p in _PROCESSED if w.startswith(p)), None)
        if hit and not any(a.startswith(hit) for a in asked):
            return False
    return True


def _noun_prefix(term: str) -> str:
    """The part of the first search word relevant() requires at the start of a receipt line."""
    words = fold(term).split()
    key = words[0] if words else ""
    return key[:4] if len(key) > 4 else key[:3]


def fold(text: str) -> str:
    """Lowercase ASCII words: 'Ítalo Supermercados' -> 'italo supermercados'."""
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_).split())


# Unlike store.search_key (made for abbreviated receipt lines), a term asked for in chat
# keeps the detail the person gave: 'carne bovina costela' must not become 'carne bovina'.
_TERM_STOP = {
    "pra", "para", "pro", "pros", "pela", "pelo", "semana", "essa", "este", "esses",
    "uns", "umas", "um", "uma", "algo", "qualquer", "tipo", "pacote", "pct",
}  # fmt: skip
MAX_TERM_WORDS = 4


def search_term(text: str) -> str:
    """Normalize a chat item into the ``termo`` the price source expects.

    Accents and punctuation off, filler and numbers dropped, up to four words. Filler
    words go away so 'carne pra semana' becomes just 'carne' -- an ambiguous term, which
    the lookup reports back instead of turning into a blind search.
    """
    words = [w for w in fold(text).split() if w.isalpha() and w not in _TERM_STOP]
    return " ".join(words[:MAX_TERM_WORDS])[:40]


def place_key(text: str) -> str:
    """Words that identify a store: 'Festval da Kennedy' -> 'festval kennedy'."""
    return " ".join([w for w in fold(text).split() if w not in _PLACE_STOP][:5])


def _word_hit(w: str, words: list[str]) -> bool:
    """``w`` is one of ``words``, or a long enough prefix of one ('compania' ~ 'companhia'
    is not, but 'condor' ~ 'condor', 'festv' ~ 'festval')."""
    return any(h == w or (len(w) >= 4 and h.startswith(w)) for h in words)


def matches_place(key: str, name: str, address: str | None) -> bool:
    """Every word of ``key`` is in the store's name or address (neighbourhood)."""
    words = fold(f"{name} {address or ''}").split()
    return bool(key) and all(_word_hit(w, words) for w in key.split())


def place_hits(key: str, rows: list[Any]) -> list[Any]:
    """Stores (code, name, address, ...) for a place: by name when some store's name has all
    the words ('fazendinha' -> 'MAXXI FAZENDINHA', not every shop in that neighbourhood),
    else by name + neighbourhood ('condor boa vista')."""
    by_name = [r for r in rows if matches_place(key, str(r[1]), None)]
    return by_name or [r for r in rows if matches_place(key, str(r[1]), r[2])]


def matches_brand(brand: str, descr: str) -> bool:
    """Receipts abbreviate ('ARROZ T JOAO 5KG'): each brand word must start a word of the
    description, or a word of it must start the brand word (3+ letters)."""
    words = fold(descr).split()
    return bool(brand) and all(
        any(d.startswith(w[:4]) or (len(d) >= 3 and w.startswith(d)) for d in words[1:])
        for w in brand.split()
    )


def is_loose(descr: str) -> bool:
    """Sold by weight (price per kg, any amount), e.g. fruit and vegetables."""
    text = descr.upper().replace(",", ".")
    return not _SIZE_RE.search(text) and bool(_LOOSE_RE.search(text))


# --- source ---


class MenorPreco:
    """Client for the Menor Preço JSON service (polite: 1 request/s, short timeouts)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            # The state's server silently drops some new connections (~1 in 5, seen from
            # home and from the VPS): give up connecting fast and retry instead of waiting.
            timeout=httpx.Timeout(20, connect=CONNECT_TIMEOUT_S),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._last = 0.0

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(CONNECT_TRIES):
            self._last = time.monotonic()
            try:
                response = self.client.get(f"{BASE_URL}/{path}", params=params)
                break
            except httpx.ConnectTimeout:
                if attempt == CONNECT_TRIES - 1:
                    raise
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def category(self, term: str, geohash: str, radius_km: int) -> int | None:
        """Use the category only when the source reports exactly one. With none the search
        goes unfiltered; with several the site asks the person, and picking the busiest one
        here hid offers of the same product filed under a different category."""
        try:
            cats = self._get("categorias", {"local": geohash, "termo": term, "raio": radius_km})
        except (httpx.HTTPError, ValueError):
            return None
        options = [c for c in cats.get("categorias") or [] if isinstance(c, dict) and "id" in c]
        return int(options[0]["id"]) if len(options) == 1 else None

    def __call__(self, term: str, geohash: str, radius_km: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        category = self.category(term, geohash, radius_km)
        for page in range(PAGES):
            params: dict[str, Any] = {"local": geohash, "termo": term, "raio": radius_km}
            params.update(data=DATA_WEEK, offset=page * PAGE_SIZE, ordem=0)
            if category is not None:
                params["categoria"] = category
            answer = self._get("produtos", params)
            items = answer.get("produtos") or []
            total = answer.get("total")
            out.extend(i for i in items if isinstance(i, dict))
            try:
                enough = total is not None and len(out) >= int(total)
            except (TypeError, ValueError):
                enough = False
            if len(items) < PAGE_SIZE or enough:
                break
        return out


# --- cache and storage ---


def _address(e: dict[str, Any]) -> str:
    parts = [e.get("tp_logr"), e.get("nm_logr"), e.get("nr_logr")]
    street = " ".join(str(p) for p in parts if p)
    bairro = e.get("bairro")
    return sanitize_text(f"{street} - {bairro}" if bairro else street, 120) or ""


def store_results(conn: sqlite3.Connection, term: str, items: list[dict[str, Any]]) -> int:
    fetched = now_utc()
    n = 0
    for p in items:
        e = p.get("estabelecimento") or {}
        code = str(e.get("codigo") or "")[:64]
        name = sanitize_text(str(e.get("nm_fan") or e.get("nm_emp") or ""), 60)
        descr = sanitize_text(str(p.get("desc") or ""), 80)
        try:
            cents = round(float(p.get("valor") or 0) * 100)
            dist = float(p.get("distkm") or 0)
        except (TypeError, ValueError):
            continue
        if not code or not name or not descr or cents <= 0:
            continue
        size = parse_size(descr)
        conn.execute(
            "INSERT INTO markets (code, name, address, dist_km, seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (code) DO UPDATE SET name = excluded.name, address = excluded.address, "
            "dist_km = excluded.dist_km, seen_at = excluded.seen_at",
            (code, name, _address(e), dist, fetched),
        )
        conn.execute(
            "INSERT OR IGNORE INTO price_obs (search, market, descr, gtin, cents, size, unit, "
            "sold_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                term,
                code,
                descr,
                str(p.get("gtin") or "")[:14] or None,
                cents,
                size[0] if size else None,
                size[1] if size else None,
                str(p.get("datahora") or fetched)[:19],
                fetched,
            ),
        )
        n += 1
    conn.execute(
        "INSERT INTO price_fetch (search, fetched_at, results) VALUES (?, ?, ?) "
        "ON CONFLICT (search) DO UPDATE SET fetched_at = excluded.fetched_at, "
        "results = excluded.results",
        (term, fetched, n),
    )
    conn.commit()
    return n


def stale(conn: sqlite3.Connection, term: str, now: datetime) -> bool:
    row = conn.execute("SELECT fetched_at FROM price_fetch WHERE search = ?", (term,)).fetchone()
    if not row:
        return True
    return now - datetime.fromisoformat(row[0]) > timedelta(hours=CACHE_HOURS)


@dataclass
class RefreshResult:
    fetched: int = 0
    cached: int = 0
    failed: list[str] = field(default_factory=list)


def refresh(
    conn: sqlite3.Connection,
    terms: list[str],
    geohash: str | None,
    radius_km: int,
    fetch: Fetcher | None,
    *,
    budget_s: float = 40.0,
) -> RefreshResult:
    """Fetch stale terms (24 h cache) within a time budget; failures are skipped."""
    result = RefreshResult()
    if not geohash or fetch is None:
        return result
    start = time.monotonic()
    now = datetime.fromisoformat(now_utc())  # same clock as the timestamps stored above
    for term in dict.fromkeys(terms):
        if not stale(conn, term, now):
            result.cached += 1
            continue
        if time.monotonic() - start > budget_s:
            result.failed.append(term)
            continue
        try:
            items = fetch(term, geohash, radius_km)
            if poisoned(items):  # anti-bot answer: never store it, stop asking for now
                log.warning("price source sent scrambled data; stopping")
                result.failed.append(term)
                break
            store_results(conn, term, items)
            result.fetched += 1
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("price fetch failed for a term: %s", type(exc).__name__)
            result.failed.append(term)
    return result


# --- choosing where to buy ---


def favorite_codes(conn: sqlite3.Connection) -> set[str] | None:
    """Stores where the household shops: marked by hand, or matching a shop_places pattern.
    None when nothing was said (then every nearby store counts)."""
    keys = [str(r[0]) for r in conn.execute("SELECT key FROM shop_places")]
    rows = conn.execute("SELECT code, name, address, favorite FROM markets").fetchall()
    if not keys and not any(r[3] for r in rows):
        return None
    codes = {str(r[0]) for r in rows if r[3]}
    for k in keys:
        codes |= {str(r[0]) for r in place_hits(k, rows)}
    return codes


def offers(
    conn: sqlite3.Connection,
    term: str,
    unit: str,
    today: date,
    shops: set[str] | EllipsisType | None = ...,
) -> list[Offer]:
    """Recent offers for a term in the household's stores (all stores if none was given).
    ``shops``: favorite_codes(conn), when the caller already has it."""
    if isinstance(shops, EllipsisType):
        shops = favorite_codes(conn)
    favorites = shops is not None
    since = (today - timedelta(days=FRESH_DAYS)).isoformat()
    since_noted = (today - timedelta(days=NOTED_DAYS)).isoformat()
    fetched_since = day_start_utc(today)
    rows = conn.execute(
        "SELECT o.market, m.name, o.descr, o.cents, o.size, o.unit, o.sold_at, o.gtin, "
        "o.fetched_at FROM price_obs o JOIN markets m ON m.code = o.market "
        "WHERE (o.search = ? OR (o.market LIKE 'nfce:%' AND o.search LIKE ?)) "
        "AND o.sold_at >= ?",
        (term, _noun_prefix(term) + "%", min(since, since_noted)),
    ).fetchall()
    out: list[Offer] = []
    receipts: list[Offer] = []
    produce = bool(term) and (term in PRODUCE or term.split()[0] in PRODUCE)
    eq = equivalence(conn, term)
    for r in rows:
        if shops is not None and r[0] not in shops:
            continue
        own = str(r[0]).startswith(RECEIPT_PREFIX)  # the household's own receipt (QR code)
        noted = str(r[0]).startswith(LOCAL_PREFIXES)
        if noted:
            if r[6] < since_noted or not r[4] or not r[5]:
                continue
            size, u, per_un = _convert(float(r[4]), str(r[5]), unit, eq)
            if u != unit:
                continue
            loose = str(r[2]).endswith(BY_WEIGHT)
            out.append(Offer(r[0], r[1], r[2], r[3], size, u, r[6], loose, per_un))
            continue
        if r[6] < since or (r[8] < fetched_since and not own) or not relevant(r[2], term):
            continue
        size, u = r[4], r[5]
        loose = is_loose(r[2])
        each = bool(_EACH_RE.search(str(r[2]).upper()))
        if u is None and each:  # "OVO UN", "BATATA LAVADA UN": one unit
            size, u = 1.0, "un"
        if u is None and produce:  # "CEBOLA": fresh produce is priced per kg
            size, u, loose = 1.0, "kg", True
        if u is None and unit == "un" and (favorites or r[7]):
            size, u = 1.0, "un"
        if not u or not size:
            continue
        size, u, per_un = _convert(float(size), u, unit, eq)
        if u != unit:
            continue
        if not favorites and not r[7] and not loose and not own:
            continue  # without favorites: barcoded or sold-by-weight only (keeps restaurants out)
        receipts.append(Offer(r[0], r[1], r[2], r[3], float(size), u, r[6], loose, per_un))
    return out + drop_outliers(receipts)


def drop_outliers(found: list[Offer]) -> list[Offer]:
    """A tiny pack of the 'same' item ('CEBOLA 10G' seasoning at R$ 299/kg next to onions at
    R$ 5/kg) is a different product: drop offers far above the median price per unit."""
    if len(found) < 4:
        return found
    per = sorted(o.per_unit for o in found)
    median = per[len(per) // 2]
    return [o for o in found if o.per_unit <= OUTLIER_FACTOR * median]


def brand_of(conn: sqlite3.Connection, term: str) -> tuple[str, str] | None:
    """(folded brand, label) bought for ``term``, if the household said."""
    row = conn.execute("SELECT brand, label FROM item_brands WHERE search = ?", (term,)).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def prefer_brand(found: list[Offer], brand: str | None) -> tuple[list[Offer], bool]:
    """Only offers of the brand when there are any; (offers, brand found)."""
    if not brand:
        return found, True
    mine = [o for o in found if matches_brand(brand, o.descr)]
    return (mine, True) if mine else (found, False)


def day_start_utc(day: date) -> str:
    """Midnight of ``day`` in São Paulo, as a UTC ISO string (how fetched_at is stored)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo("America/Sao_Paulo"))
    return start.astimezone(UTC).isoformat(timespec="seconds")


def poisoned(items: list[dict[str, Any]]) -> bool:
    """Most items with a one-word description or a very long random store id."""
    if not items:
        return False
    codes = [str((i.get("estabelecimento") or {}).get("codigo") or "") for i in items]
    bare = sum(len(str(i.get("desc") or "").split()) <= 1 for i in items)
    long = sum(len(c) >= POISONED_CODE_LEN for c in codes)
    return max(bare, long) * 2 > len(items)


def category_of(conn: sqlite3.Connection, term: str) -> int | None:
    """Cached category id, or 0 when we already learned there is no single one (never sent
    to the source). None means it still has to be looked up."""
    row = conn.execute("SELECT category FROM price_category WHERE search = ?", (term,)).fetchone()
    return int(row[0]) if row else None


def set_category(conn: sqlite3.Connection, term: str, category: int) -> None:
    conn.execute(
        "INSERT INTO price_category (search, category, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (search) DO UPDATE SET category = excluded.category, "
        "updated_at = excluded.updated_at",
        (term, category, now_utc()),
    )


def fetched_today(conn: sqlite3.Connection, term: str, today: date) -> bool:
    row = conn.execute("SELECT fetched_at FROM price_fetch WHERE search = ?", (term,)).fetchone()
    return bool(row) and str(row[0]) >= day_start_utc(today)


def note(
    conn: sqlite3.Connection,
    term: str,
    market: str,
    descr: str,
    cents: int,
    size: float,
    unit: str,
    day: date,
) -> None:
    """A price someone in the house saw (replaces the same product/market/day)."""
    conn.execute(
        "INSERT OR REPLACE INTO price_obs (search, market, descr, gtin, cents, size, unit, "
        "sold_at, fetched_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        (term, market, descr, cents, size, unit, day.isoformat(), now_utc()),
    )
    conn.commit()


def cost(need: Need, offer: Offer) -> int:
    """Cheapest way to cover ``need`` with whole packages of ``offer`` (or by weight)."""
    if offer.loose:
        return max(1, math.ceil(need.qty / offer.size * offer.cents))
    packs = max(1, math.ceil(need.qty / offer.size - 1e-9))
    return packs * offer.cents


def best_by_market(need: Need, found: list[Offer]) -> dict[str, tuple[int, Offer]]:
    best: dict[str, tuple[int, Offer]] = {}
    for o in found:
        c = cost(need, o)
        if o.market not in best or c < best[o.market][0]:
            best[o.market] = (c, o)
    return best


@dataclass
class Plan:
    markets: list[str]
    names: dict[str, str]
    total: int
    lines: list[tuple[Need, Offer | None, int | None]]
    missing: list[Need]


def choose(needs: list[Need], per_need: list[dict[str, tuple[int, Offer]]]) -> Plan | None:
    """One market, or two when the second saves enough. Items without prices are listed apart."""
    markets = sorted({m for b in per_need for m in b})
    if not markets:
        return None
    names = {o.market: o.name for b in per_need for _, o in b.values()}

    def evaluate(chosen: tuple[str, ...]) -> tuple[int, int]:
        covered, total = 0, 0
        for b in per_need:
            options = [b[m][0] for m in chosen if m in b]
            if options:
                covered += 1
                total += min(options)
        return covered, total

    singles = sorted(((evaluate((m,)), (m,)) for m in markets), key=lambda x: (-x[0][0], x[0][1]))
    best_score, first = singles[0]
    best: tuple[str, ...] = first
    for pair in combinations(markets, 2):
        score = evaluate(pair)
        saving = best_score[1] - score[1]
        if score[0] > best_score[0] or (
            score[0] == best_score[0]
            and saving >= max(PAIR_MIN_SAVING, PAIR_MIN_SHARE * best_score[1])
        ):
            best_score, best = score, pair
    lines: list[tuple[Need, Offer | None, int | None]] = []
    missing = []
    for need, b in zip(needs, per_need, strict=True):
        options = [b[m] for m in best if m in b]
        if options:
            c, o = min(options, key=lambda x: x[0])
            lines.append((need, o, c))
        else:
            lines.append((need, None, None))
            missing.append(need)
    return Plan(list(best), names, best_score[1], lines, missing)


def nearby_markets(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    """Shops near home (OpenStreetMap or noted by hand), favorites then nearest first."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT m.code, m.name, m.address, m.dist_km, m.favorite, m.kind, COUNT(o.id) AS n "
        "FROM markets m LEFT JOIN price_obs o ON o.market = m.code "
        "WHERE m.code LIKE 'osm:%' OR m.code LIKE 'manual:%' OR m.code LIKE 'nfce:%' "
        "OR o.gtin IS NOT NULL "
        "GROUP BY m.code ORDER BY m.favorite DESC, m.dist_km IS NULL, m.dist_km LIMIT ?",
        (limit,),
    ).fetchall()


def set_favorite(
    conn: sqlite3.Connection, name_or_code: str, favorite: bool, by: str | None = None
) -> list[str]:
    """Mark where the household shops. A name is kept as a pattern (shop_places) that matches
    every store from the receipts and the map with those words in its name or neighbourhood
    ('condor boa vista', 'italo'); a code marks one store. Returns the stores matched."""
    raw = name_or_code.strip()
    one = conn.execute("SELECT code, name FROM markets WHERE code = ?", (raw,)).fetchone()
    if one:
        conn.execute("UPDATE markets SET favorite = ? WHERE code = ?", (int(favorite), one[0]))
        conn.commit()
        return [str(one[1])]
    key = place_key(raw)
    if not key:
        return []
    rows = conn.execute("SELECT code, name, address FROM markets").fetchall()
    hits = place_hits(key, rows)
    if favorite:
        conn.execute(
            "INSERT INTO shop_places (key, label, created_at, created_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET label = excluded.label",
            (key, sanitize_text(raw, 60) or key, now_utc(), by),
        )
    else:
        conn.execute("DELETE FROM shop_places WHERE key = ?", (key,))
    for r in hits:
        conn.execute("UPDATE markets SET favorite = ? WHERE code = ?", (int(favorite), r[0]))
    conn.commit()
    return sorted({str(r[1]) for r in hits})


def shop_places(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT label FROM shop_places ORDER BY created_at")]

"""Grocery prices from NFC-e based sources, and where to buy the shopping list.

Source today: Menor Preço (Nota Paraná, PR), the public price search of the
Paraná state treasury. Its JSON service is NOT documented; it may change, so it
is isolated here, cached for 24 h per search term, rate-limited and never on the
critical path (no prices -> the list still works, just without "where").

What is sent to the source: the search term and the household's coarse location
(geohash with ~1 km precision) and radius. Nothing else.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from typing import Any, Protocol

import httpx

from grana_mcp.kitchen.store import Need
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

log = logging.getLogger(__name__)

BASE_URL = "https://menorpreco.notaparana.pr.gov.br/api/v1"
USER_AGENT = "grana-mcp (uso pessoal; lista de compras da casa)"
CACHE_HOURS = 24
FRESH_DAYS = 10  # ignore receipt prices sold longer ago than this
NOTED_DAYS = 60  # prices the household noted down last longer
LOCAL_PREFIXES = ("osm:", "manual:")  # markets not from the receipt source
BY_WEIGHT = ("por kg", "por l")  # suffix of a noted price that is per kg/l, any amount
CONNECT_TIMEOUT_S = 3.0
CONNECT_TRIES = 4
PAGES = 2  # 40 results per page
MIN_INTERVAL_S = 1.0
PAIR_MIN_SAVING = 15_00  # a second market must save at least R$ 15 ...
PAIR_MIN_SHARE = 0.05  # ... and 5% of the basket

_SIZE_RE = re.compile(
    r"(?:(\d+)\s*X\s*)?(\d+(?:[.,]\d+)?)\s*(KG|KGS|G|GR|GRS|L|LT|LTS|ML|UN|UND|UNID|UNIDADES)\b"
)
_DOZEN_RE = re.compile(r"\b(DZ|DUZIA)\b")
_BOX_RE = re.compile(r"\b(?:C|CX|C/)\s?(\d{1,3})\b")  # "OVOS BCO C10": box of 10
_LOOSE_RE = re.compile(r"(?<![\d.])\s*\b(KG|KILO)\b")  # "TOMATE KG": priced per kg


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

    @property
    def per_unit(self) -> float:
        return self.cents / self.size


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
    is not. The first word must share a prefix with the first search word."""
    words = term.split()
    first = re.split(r"[^A-Z]+", descr.upper().strip(), maxsplit=1)[0]
    if not words or not first:
        return False
    key = words[0].upper()
    prefix = key[:4] if len(key) > 4 else key[:3]
    return first.startswith(prefix)


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
        """The category with most products for the term ('leite' -> 'Leite e derivados'),
        so cheap unrelated items (chocolate, snacks) don't crowd the results."""
        try:
            cats = self._get("categorias", {"local": geohash, "termo": term, "raio": radius_km})
        except (httpx.HTTPError, ValueError):
            return None
        options = [c for c in cats.get("categorias") or [] if isinstance(c, dict) and "id" in c]
        if not options:
            return None
        return int(max(options, key=lambda c: int(c.get("qtd") or 0))["id"])

    def __call__(self, term: str, geohash: str, radius_km: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        category = self.category(term, geohash, radius_km)
        for page in range(PAGES):
            params: dict[str, Any] = {"local": geohash, "termo": term, "raio": radius_km}
            params.update(data=-1, offset=page * 40, ordem=0)
            if category is not None:
                params["categoria"] = category
            items = self._get("produtos", params).get("produtos") or []
            out.extend(i for i in items if isinstance(i, dict))
            if len(items) < 40:
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
    now = datetime.now(UTC)
    for term in dict.fromkeys(terms):
        if not stale(conn, term, now):
            result.cached += 1
            continue
        if time.monotonic() - start > budget_s:
            result.failed.append(term)
            continue
        try:
            store_results(conn, term, fetch(term, geohash, radius_km))
            result.fetched += 1
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("price fetch failed for a term: %s", type(exc).__name__)
            result.failed.append(term)
    return result


# --- choosing where to buy ---


def offers(conn: sqlite3.Connection, term: str, unit: str, today: date) -> list[Offer]:
    """Recent offers for a term in favorite markets (all markets if none is favorite)."""
    favorites = conn.execute("SELECT COUNT(*) FROM markets WHERE favorite = 1").fetchone()[0]
    since = (today - timedelta(days=FRESH_DAYS)).isoformat()
    since_noted = (today - timedelta(days=NOTED_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT o.market, m.name, o.descr, o.cents, o.size, o.unit, o.sold_at, o.gtin "
        "FROM price_obs o JOIN markets m ON m.code = o.market "
        "WHERE o.search = ? AND o.sold_at >= ? AND (? = 0 OR m.favorite = 1)",
        (term, since_noted, favorites),
    ).fetchall()
    out = []
    for r in rows:
        noted = str(r[0]).startswith(LOCAL_PREFIXES)
        if noted:
            if r[5] != unit or not r[4]:
                continue
            loose = str(r[2]).endswith(BY_WEIGHT)
            out.append(Offer(r[0], r[1], r[2], r[3], float(r[4]), r[5], r[6], loose))
            continue
        if r[6] < since or not relevant(r[2], term):
            continue
        size, u = r[4], r[5]
        if u is None and unit == "un" and (favorites or r[7]):
            size, u = 1.0, "un"
        if u != unit or not size:
            continue
        if not favorites and not r[7] and not is_loose(r[2]):
            continue  # without favorites: barcoded or sold-by-weight only (keeps restaurants out)
        out.append(Offer(r[0], r[1], r[2], r[3], float(size), u, r[6], is_loose(r[2])))
    return out


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
        "WHERE m.code LIKE 'osm:%' OR m.code LIKE 'manual:%' OR o.gtin IS NOT NULL "
        "GROUP BY m.code ORDER BY m.favorite DESC, m.dist_km IS NULL, m.dist_km LIMIT ?",
        (limit,),
    ).fetchall()


def set_favorite(conn: sqlite3.Connection, name_or_code: str, favorite: bool) -> list[str]:
    key = name_or_code.strip()
    rows = conn.execute(
        "SELECT code, name FROM markets WHERE code = ? OR lower(name) LIKE lower(?)",
        (key, f"%{key}%"),
    ).fetchall()
    for code, _ in rows:
        conn.execute("UPDATE markets SET favorite = ? WHERE code = ?", (int(favorite), code))
    conn.commit()
    return [str(r[1]) for r in rows]

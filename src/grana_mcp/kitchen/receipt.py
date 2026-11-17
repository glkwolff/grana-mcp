"""The household's own NFC-e receipts (Paraná): the QR code on the receipt is a link to the
public page of that receipt at the state's tax office, with the store, the date and every
item with its price. Reading it gives exact prices paid, in the right store and day.

The QR text is untrusted: only its ``p`` parameter is used, checked against the NFC-e key
format, and the URL is rebuilt with a fixed host. The receipt key is stored only as a salted
hash (to skip receipts already read); the store's CNPJ only as a salted hash too.
"""

from __future__ import annotations

import html as html_lib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from grana_mcp.kitchen import prices
from grana_mcp.kitchen.store import KitchenError
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import hash_doc, now_utc

CONSULT_URL = "https://www.fazenda.pr.gov.br/nfce/qrcode"
UF_PR = "41"
MODEL_NFCE = "65"
MAX_PAGE = 2_000_000
MAX_ITEMS = 300
TIMEOUT_S = 20.0
PREFIX = prices.RECEIPT_PREFIX

_P_RE = re.compile(r"^(\d{44})(\|[0-9A-Za-z|]{1,200})?$")
_ISSUED_RE = re.compile(
    r"Emiss[ãa]o:\s*(?:</strong>)?\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})", re.I
)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?")


class Fetcher(Protocol):
    def __call__(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Line:
    descr: str
    qty: float
    unit: str  # as printed: UN, KG, L, PCT...
    unit_cents: int
    total_cents: int


@dataclass(frozen=True, slots=True)
class Receipt:
    store: str
    address: str | None
    cnpj: str
    issued: datetime
    lines: list[Line]
    total_cents: int | None


def parse_qr(text: str) -> tuple[str, str]:
    """QR text (or the link copied from it) -> (44-digit key, URL to fetch)."""
    raw = str(text or "").strip()
    if not raw:
        raise KitchenError("mande o QR Code ou o link da nota")
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        host = parts.netloc.lower().split(":")[0]
        if not host.endswith(".pr.gov.br"):
            raise KitchenError("só notas do Paraná (link da Sefaz-PR)")
        values = parse_qs(parts.query).get("p") or []
        p = values[0] if values else ""
    else:
        p = raw
    p = p.strip()
    m = _P_RE.match(p)
    if not m:
        raise KitchenError("isso não parece o QR Code de uma NFC-e")
    key = m.group(1)
    if key[:2] != UF_PR:
        raise KitchenError("só notas do Paraná por enquanto")
    if key[20:22] != MODEL_NFCE:
        raise KitchenError("isso não é uma NFC-e (cupom de mercado)")
    if not m.group(2):
        raise KitchenError("use o QR Code da nota (só a chave não basta para consultar)")
    return key, f"{CONSULT_URL}?p={quote(p, safe='|')}"


def fetch_page(url: str) -> str:
    if not url.startswith(CONSULT_URL + "?"):
        raise KitchenError("endereço inválido")
    try:
        with httpx.Client(
            timeout=TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": prices.USER_AGENT},
        ) as client:
            res = client.get(url)
    except httpx.HTTPError:
        raise KitchenError("a Sefaz-PR não respondeu agora; tente de novo daqui a pouco") from None
    host = (res.url.host or "").lower()
    if not host.endswith(".pr.gov.br"):
        raise KitchenError("a Sefaz respondeu de um endereço inesperado")
    if res.status_code != 200:
        raise KitchenError(f"a Sefaz-PR respondeu {res.status_code}")
    return res.text[:MAX_PAGE]


def _text(fragment: str) -> str:
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", fragment)).split())


def _cents(value: str) -> int | None:
    m = _NUM_RE.search(value or "")
    if not m:
        return None
    try:
        return round(float(m.group(0).replace(".", "").replace(",", ".")) * 100)
    except ValueError:
        return None


def _qty(value: str) -> float | None:
    m = _NUM_RE.search(value or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _span(chunk: str, cls: str) -> str:
    m = re.search(
        rf'<(span|div|td)[^>]*class="(?:[^"]*\s)?{cls}\d*(?:\s[^"]*)?"[^>]*>(.*?)</\1>',
        chunk,
        re.S | re.I,
    )
    return _text(m.group(2)) if m else ""


def _after_label(value: str) -> str:
    """'Qtde.: 1,5' -> '1,5'; 'Vl. Unit.: 8,99' -> '8,99'."""
    return value.split(":", 1)[1].strip() if ":" in value else value


def parse_page(page: str) -> Receipt:
    """The public NFC-e page (portal layout: div.txtTopo, table#tabResult...)."""
    err = re.search(r'id="flashMessage".*?<font[^>]*>(.*?)</font>', page, re.S | re.I)
    if err and "tabResult" not in page:
        msg = _text(err.group(1)).replace("Atenção", "").strip(" .")
        raise KitchenError(f"a Sefaz-PR não mostrou a nota: {sanitize_text(msg, 120)}")
    table = re.search(r'id="tabResult"[^>]*>(.*?)</table>', page, re.S | re.I)
    if not table:
        raise KitchenError("não consegui ler a nota (a página da Sefaz-PR veio diferente)")
    lines: list[Line] = []
    for chunk in re.split(r"<tr\b", table.group(1), flags=re.I)[1:]:
        if "RvlUnit" not in chunk:
            continue
        descr = sanitize_text(_span(chunk, "txtTit"), 80)
        qty = _qty(_after_label(_span(chunk, "Rqtd")))
        unit = _after_label(_span(chunk, "RUN")).upper()[:6]
        unit_cents = _cents(_after_label(_span(chunk, "RvlUnit")))
        total = _cents(_span(chunk, "valor"))
        if not descr or not qty or not unit_cents or unit_cents <= 0:
            continue
        lines.append(Line(descr, qty, unit, unit_cents, total or round(qty * unit_cents)))
        if len(lines) >= MAX_ITEMS:
            break
    if not lines:
        raise KitchenError("a nota não tem itens que eu consiga ler")
    head = re.search(r'class="txtTopo"[^>]*>(.*?)</div>', page, re.S | re.I)
    store = sanitize_text(_text(head.group(1)), 60) if head else None
    region = page[head.end() : head.end() + 3000] if head else ""
    region = region.split("tabResult", 1)[0]
    texts = re.findall(r'<div[^>]*class="text"[^>]*>(.*?)</div>', region, re.S | re.I)
    cnpj = ""
    address = None
    for t in (_text(x) for x in texts):
        if "CNPJ" in t.upper():
            cnpj = re.sub(r"\D", "", t)[:14]
        elif t:  # 'R X, 164, , NOVO MUNDO, Curitiba, PR'
            parts = [x.strip() for x in t.split(",") if x.strip()]
            address = sanitize_text(", ".join(parts), 120)
    issued_m = _ISSUED_RE.search(page)
    if not issued_m:
        raise KitchenError("não achei a data da nota")
    issued = datetime.strptime(" ".join(issued_m.groups()), "%d/%m/%Y %H:%M:%S")
    total_m = re.search(r'class="[^"]*totalNumb[^"]*txtMax[^"]*"[^>]*>(.*?)<', page, re.S | re.I)
    return Receipt(
        store=store or "Mercado (NFC-e)",
        address=address,
        cnpj=cnpj,
        issued=issued,
        lines=lines,
        total_cents=_cents(total_m.group(1)) if total_m else None,
    )


def _size(line: Line) -> tuple[float | None, str | None]:
    """Package size of a line: by-weight units are priced per kg/l; else from the text."""
    if line.unit in ("KG", "KGS", "QUILO"):
        return 1.0, "kg"
    if line.unit in ("L", "LT", "LTS", "LITRO"):
        return 1.0, "l"
    size = prices.parse_size(line.descr)
    return (size[0], size[1]) if size else (None, None)


def search_word(descr: str) -> str:
    """Receipt items are filed under their first word (the product noun); offers() matches
    them to any search term with relevant()."""
    words = prices.fold(descr).split()
    return words[0] if words else ""


def seen(conn: sqlite3.Connection, key_hash: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row: sqlite3.Row | None = conn.execute(
        "SELECT day, items, store FROM receipts WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    return row


def store_receipt(
    conn: sqlite3.Connection, r: Receipt, key_hash: str, salt: bytes, by: str | None
) -> tuple[str, int]:
    """Save the store (marked as one of the household's) and every line's price.
    Returns (market code, lines stored)."""
    ident = hash_doc(r.cnpj or prices.fold(r.store), salt)
    code = PREFIX + ident[:24]
    now = now_utc()
    conn.execute(
        "INSERT INTO markets (code, name, address, dist_km, favorite, seen_at) "
        "VALUES (?, ?, ?, NULL, 1, ?) ON CONFLICT (code) DO UPDATE SET name = excluded.name, "
        "address = COALESCE(excluded.address, markets.address), favorite = 1, "
        "seen_at = excluded.seen_at",
        (code, r.store, r.address, now),
    )
    sold_at = r.issued.isoformat(timespec="seconds")
    n = 0
    for line in r.lines:
        word = search_word(line.descr)
        if not word:
            continue
        size, unit = _size(line)
        descr = line.descr
        if unit == "kg" and size == 1.0 and not prices.parse_size(descr):
            descr = f"{descr} KG"  # sold by weight: the unit price is per kg (see is_loose)
        conn.execute(
            "INSERT OR REPLACE INTO price_obs (search, market, descr, gtin, cents, size, unit, "
            "sold_at, fetched_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (word, code, descr, line.unit_cents, size, unit, sold_at, now),
        )
        n += 1
    conn.execute(
        "INSERT INTO receipts (key_hash, market, day, items, total_cents, store, created_at, by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (key_hash, code, r.issued.date().isoformat(), n, r.total_cents, r.store, now, by),
    )
    conn.commit()
    return code, n

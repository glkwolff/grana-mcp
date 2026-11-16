"""Food shops near home, from OpenStreetMap (Overpass API).

Only the center of the household's coarse geohash cell (~1 km) and a radius are sent.
Names and addresses are third-party data: sanitized, never instructions.
"""

from __future__ import annotations

import itertools
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from grana_mcp import __version__
from grana_mcp.kitchen import geo
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

# Public Overpass instances, tried in order (the main one is often busy: 429/502/504).
_MAIN = "https://overpass-api.de/api/interpreter"
OVERPASS_URLS = (  # the main instance between each mirror
    _MAIN,
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    _MAIN,
    "https://overpass.private.coffee/api/interpreter",
    _MAIN,
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = f"grana-mcp/{__version__} (+https://github.com/glkwolff/grana-mcp)"
KINDS = {
    "supermarket": "supermercado",
    "wholesale": "atacado",
    "convenience": "mercearia",
    "greengrocer": "hortifrúti",
    "butcher": "açougue",
    "seafood": "peixaria",
    "bakery": "padaria",
    "deli": "empório",
    "health_food": "produtos naturais",
    "farm": "produtor",
    "marketplace": "feira",
}
BUDGET_S = 150.0
REQUEST_TIMEOUT_S = 65.0  # a busy instance takes 30-60 s
MAX_RADIUS_KM = 4
# Where a weekly shop happens: nearest 30 kept (wholesale always: worth an Uber).
MAIN_KINDS = {"supermercado", "atacado", "hortifrúti", "açougue", "peixaria", "feira"}
KEEP_MAIN = 30
KEEP_OTHER = 8  # bakeries, corner shops, health food: only the nearest few

# (lat, lon, radius_m) -> raw Overpass elements
Fetcher = Callable[[float, float, int], list[dict[str, Any]]]


def query(lat: float, lon: float, radius_m: int) -> str:
    shops = "|".join(k for k in KINDS if k != "marketplace")
    around = f"(around:{radius_m},{lat:.5f},{lon:.5f})"
    return (
        f'[out:json][timeout:60];(nwr[shop~"^({shops})$"][name]{around};'
        f"nwr[amenity=marketplace][name]{around};);out center tags;"
    )


def overpass(
    lat: float, lon: float, radius_m: int, *, budget_s: float = BUDGET_S
) -> list[dict[str, Any]]:
    """Overpass query with exponential backoff across instances (busy servers answer
    429/502/504 and ask clients to back off and retry)."""
    body = {"data": query(lat, lon, radius_m)}
    deadline = time.monotonic() + budget_s
    wait = 2.0
    last: Exception = httpx.HTTPError("overpass")
    with httpx.Client(timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
        for attempt in itertools.count():
            url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
            try:
                r = client.post(url, data=body)
                r.raise_for_status()
                data = r.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
            if time.monotonic() + wait > deadline:
                raise last
            time.sleep(wait)
            wait *= 2
    elements = data.get("elements") if isinstance(data, dict) else None
    return [e for e in elements or [] if isinstance(e, dict)]


@dataclass
class Place:
    code: str
    name: str
    kind: str
    address: str
    dist_km: float


def parse(elements: list[dict[str, Any]], home: tuple[float, float]) -> list[Place]:
    out: dict[str, Place] = {}
    for e in elements:
        tags = e.get("tags") or {}
        kind = KINDS.get(str(tags.get("shop") or tags.get("amenity") or ""))
        name = sanitize_text(str(tags.get("name") or ""), 60)
        center = e.get("center") or e
        try:
            point = (float(center["lat"]), float(center["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not kind or not name or e.get("type") not in ("node", "way", "relation"):
            continue
        street = " ".join(str(tags[k]) for k in ("addr:street", "addr:housenumber") if k in tags)
        hood = tags.get("addr:suburb")
        address = sanitize_text(f"{street} - {hood}" if street and hood else street, 120) or ""
        code = f"osm:{e['type'][0]}{e.get('id')}"
        out[code] = Place(code, name, kind, address, round(geo.distance_km(home, point), 2))
    return sorted(out.values(), key=lambda p: p.dist_km)


def store(conn: sqlite3.Connection, places: list[Place]) -> int:
    main = [p for p in places if p.kind in MAIN_KINDS]
    other = [p for p in places if p.kind not in MAIN_KINDS]
    keep = [p for i, p in enumerate(main) if i < KEEP_MAIN or p.kind == "atacado"]
    keep += other[:KEEP_OTHER]
    now = now_utc()
    conn.executemany(
        "INSERT INTO markets (code, name, address, dist_km, kind, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (code) DO UPDATE SET name = excluded.name, "
        "address = excluded.address, dist_km = excluded.dist_km, kind = excluded.kind, "
        "seen_at = excluded.seen_at",
        [(p.code, p.name, p.address, p.dist_km, p.kind, now) for p in keep],
    )
    conn.commit()
    return len(keep)


def discover(conn: sqlite3.Connection, geohash: str, radius_km: int, fetch: Fetcher) -> int:
    home = geo.decode(geohash)
    radius_m = int(min(max(radius_km, 1), MAX_RADIUS_KM) * 1000)
    return store(conn, parse(fetch(home[0], home[1], radius_m), home))

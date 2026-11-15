"""Geohash encoding (the price source takes a geohash). Precision 6 ~ 1.2 x 0.6 km:
the household location is kept coarse on purpose."""

from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 6) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordenadas inválidas")
    lat_r, lon_r = [-90.0, 90.0], [-180.0, 180.0]
    out: list[str] = []
    bit, ch, even = 0, 0, True
    while len(out) < precision:
        rng, val = (lon_r, lon) if even else (lat_r, lat)
        mid = (rng[0] + rng[1]) / 2
        if val > mid:
            ch |= 1 << (4 - bit)
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def decode(gh: str) -> tuple[float, float]:
    """Center (lat, lon) of a geohash cell."""
    lat_r, lon_r = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for c in gh.lower():
        idx = _BASE32.find(c)
        if idx < 0:
            raise ValueError("geohash inválido")
        for shift in range(4, -1, -1):
            rng = lon_r if even else lat_r
            mid = (rng[0] + rng[1]) / 2
            if idx >> shift & 1:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return (lat_r[0] + lat_r[1]) / 2, (lon_r[0] + lon_r[1]) / 2


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(h))

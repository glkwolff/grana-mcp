"""Recurring charges (subscriptions) detection. Pure functions + one read-only query.

A series is recurring when, for the same merchant key:

* there are at least ``MIN_OCCURRENCES`` charges whose amounts are within
  ``TOLERANCE`` (±10%) of the most recent charge;
* the gaps between consecutive charges in that series are "about a month"
  (``MIN_GAP``..``MAX_GAP`` days), at most one charge per gap;
* at least ``MIN_SHARE`` of the merchant's charges in that time span belong to
  the series (a supermarket visited weekly is not a subscription);
* card installments ("parcela x/y") are not subscriptions and are ignored.

A series is ``active`` when its last charge is at most ``ACTIVE_DAYS`` old.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from grana_mcp.pipeline.categorize import norm
from grana_mcp.privacy.redact import looks_like_person_transfer

MIN_OCCURRENCES = 3
TOLERANCE = 0.10
MIN_GAP = 20
MAX_GAP = 40
ACTIVE_DAYS = 45
# Share of the merchant's charges (in the series' time span) that belong to the series.
# Frequent merchants (supermarket, rides) form monthly chains by chance; a
# subscription is (almost) the only thing charged by that merchant.
MIN_SHARE = 0.6

_CODE_TOKEN_RE = re.compile(r"^[a-z]+\d[a-z0-9]*$")  # "p46fb", "raia2153", "lj27"


def merchant_key(clean_merchant: str | None) -> str:
    """Stable key for grouping: normalized merchant without reference codes."""
    tokens = [t for t in norm(clean_merchant).split() if not _CODE_TOKEN_RE.match(t)]
    return " ".join(tokens)


@dataclass(frozen=True, slots=True)
class Charge:
    key: str
    label: str
    category: str
    posted: date
    cents: int  # positive spending


@dataclass(frozen=True, slots=True)
class Series:
    key: str
    label: str
    category: str
    occurrences: int
    last_date: date
    last_cents: int
    median_cents: int
    previous_cents: int | None
    next_expected: date
    active: bool

    @property
    def price_changed(self) -> bool:
        return self.previous_cents is not None and self.previous_cents != self.last_cents


def _within(cents: int, ref: int) -> bool:
    return abs(cents - ref) <= ref * TOLERANCE


def detect(charges: Iterable[Charge], today: date) -> list[Series]:
    groups: dict[str, list[Charge]] = defaultdict(list)
    for c in charges:
        if c.key:
            groups[c.key].append(c)
    found: list[Series] = []
    for key, items in groups.items():
        series = _series_from(sorted(items, key=lambda c: (c.posted, c.cents)))
        if len(series) < MIN_OCCURRENCES:
            continue
        span = [c for c in items if series[0].posted <= c.posted <= series[-1].posted]
        if len(series) < MIN_SHARE * len(span):
            continue
        last = series[-1]
        gaps = [(b.posted - a.posted).days for a, b in pairwise(series)]
        avg_gap = round(statistics.mean(gaps))
        found.append(
            Series(
                key=key,
                label=last.label,
                category=last.category,
                occurrences=len(series),
                last_date=last.posted,
                last_cents=last.cents,
                median_cents=int(statistics.median(c.cents for c in series)),
                previous_cents=series[-2].cents,
                next_expected=last.posted + timedelta(days=avg_gap),
                active=(today - last.posted).days <= ACTIVE_DAYS,
            )
        )
    return sorted(found, key=lambda s: (not s.active, -s.last_cents, s.key))


def _series_from(items: Sequence[Charge]) -> list[Charge]:
    """Walk back from the most recent charge, keeping charges with a similar
    amount spaced about a month apart. Stops at the first gap that is too long."""
    if not items:
        return []
    ref = items[-1].cents
    chain = [items[-1]]
    for c in reversed(items[:-1]):
        if not _within(c.cents, ref):
            continue
        gap = (chain[-1].posted - c.posted).days
        if gap < MIN_GAP:
            continue  # same-month duplicate/extra purchase: skip it
        if gap > MAX_GAP:
            break
        chain.append(c)
    return list(reversed(chain))


def load_charges(
    conn: sqlite3.Connection, today: date, where_extra: str = "", params: Sequence[object] = ()
) -> list[Charge]:
    """Spending rows of the last 400 days (no installments, no excluded, not future)."""
    start = today - timedelta(days=400)
    rows = conn.execute(
        "SELECT t.posted_date, t.amount_cents, t.description_clean, t.description_raw, "
        "COALESCE(t.category, 'Outros') AS category, t.counterpart_doc_type, "
        "t.counterpart_doc_hash, t.operation_type "
        "FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN people pe ON pe.id = a.owner_person_id "
        "LEFT JOIN profiles p ON p.id = t.profile_id "
        "WHERE t.amount_cents < 0 AND t.excluded_reason IS NULL "
        "AND t.installment_total IS NULL AND t.posted_date >= ? AND t.posted_date <= ?"
        + where_extra,
        [start.isoformat(), today.isoformat(), *params],
    ).fetchall()
    out = []
    for r in rows:
        if looks_like_person_transfer(
            r[3], counterpart_doc_hash=r[6], counterpart_doc_type=r[5], operation_type=r[7]
        ):
            continue  # transfers to natural persons: never listed by name
        label = r[2] or r[3] or ""
        out.append(
            Charge(
                key=merchant_key(label),
                label=label,
                category=r[4],
                posted=date.fromisoformat(r[0]),
                cents=-r[1],
            )
        )
    return out

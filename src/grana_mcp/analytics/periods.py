"""One parser for every ``period`` argument of the MCP tools.

Accepted values (``today`` is the São Paulo calendar day):

* ``"YYYY-MM"``                     that calendar month;
* ``"this_month"`` / ``"last_month"``;
* ``"last_N_months"`` (1 <= N <= 24) the current month plus the N-1 before it
  (the current month is month-to-date);
* ``{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`` inclusive, at most 3 years.

Dates are inclusive. Rows dated after ``today`` (scheduled card installments)
are filtered by the queries, not here.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

MAX_MONTHS = 24
MAX_RANGE_DAYS = 3 * 366

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_LAST_N_RE = re.compile(r"^last_(\d{1,2})_months?$")


class PeriodError(ValueError):
    """Invalid ``period`` (message is safe to return to the client)."""


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date  # inclusive
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "label": self.label}


def month_bounds(year: int, month: int) -> tuple[date, date]:
    if not 1 <= month <= 12 or not 1900 <= year <= 9999:
        raise PeriodError(f"invalid month: {year:04d}-{month:02d}")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def parse_period(value: Any, today: date) -> Period:
    if isinstance(value, dict):
        return _parse_range(value)
    if not isinstance(value, str):
        raise PeriodError("period must be a string or an object {from, to}")
    text = value.strip().lower()
    if m := _MONTH_RE.match(text):
        start, end = month_bounds(int(m.group(1)), int(m.group(2)))
        return Period(start, end, text)
    if text == "this_month":
        start, end = month_bounds(today.year, today.month)
        return Period(start, end, start.strftime("%Y-%m"))
    if text == "last_month":
        y, mo = shift_month(today.year, today.month, -1)
        start, end = month_bounds(y, mo)
        return Period(start, end, start.strftime("%Y-%m"))
    if m := _LAST_N_RE.match(text):
        n = int(m.group(1))
        if not 1 <= n <= MAX_MONTHS:
            raise PeriodError(f"last_N_months: N must be between 1 and {MAX_MONTHS}")
        y, mo = shift_month(today.year, today.month, -(n - 1))
        start = date(y, mo, 1)
        _, end = month_bounds(today.year, today.month)
        return Period(start, end, f"last_{n}_months")
    raise PeriodError(
        "period must be 'YYYY-MM', 'this_month', 'last_month', 'last_N_months' "
        "or {'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'}"
    )


def _parse_range(value: dict[str, Any]) -> Period:
    raw_from, raw_to = value.get("from"), value.get("to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise PeriodError("period object needs 'from' and 'to' as YYYY-MM-DD")
    try:
        start, end = date.fromisoformat(raw_from.strip()), date.fromisoformat(raw_to.strip())
    except ValueError:
        raise PeriodError("period dates must be YYYY-MM-DD") from None
    if start > end:
        raise PeriodError("period 'from' is after 'to'")
    if (end - start).days > MAX_RANGE_DAYS:
        raise PeriodError("period longer than 3 years")
    return Period(start, end, f"{start.isoformat()}..{end.isoformat()}")

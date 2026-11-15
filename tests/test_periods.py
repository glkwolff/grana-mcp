from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from grana_mcp.analytics.periods import Period, PeriodError, parse_period, shift_month

TODAY = date(2026, 11, 15)


@pytest.mark.parametrize(
    ("value", "start", "end", "label"),
    [
        ("2026-10", date(2026, 10, 1), date(2026, 10, 31), "2026-10"),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29), "2024-02"),
        (" 2026-12 ", date(2026, 12, 1), date(2026, 12, 31), "2026-12"),
        ("this_month", date(2026, 11, 1), date(2026, 11, 30), "2026-11"),
        ("last_month", date(2026, 10, 1), date(2026, 10, 31), "2026-10"),
        ("LAST_MONTH", date(2026, 10, 1), date(2026, 10, 31), "2026-10"),
        ("last_3_months", date(2026, 9, 1), date(2026, 11, 30), "last_3_months"),
        ("last_1_month", date(2026, 11, 1), date(2026, 11, 30), "last_1_months"),
        ("last_12_months", date(2025, 12, 1), date(2026, 11, 30), "last_12_months"),
        (
            {"from": "2026-10-05", "to": "2026-10-20"},
            date(2026, 10, 5),
            date(2026, 10, 20),
            "2026-10-05..2026-10-20",
        ),
    ],
)
def test_parse_period(value: Any, start: date, end: date, label: str) -> None:
    assert parse_period(value, TODAY) == Period(start, end, label)


def test_last_month_in_january_wraps_year() -> None:
    p = parse_period("last_month", date(2027, 1, 10))
    assert (p.start, p.end) == (date(2026, 12, 1), date(2026, 12, 31))


def test_shift_month() -> None:
    assert shift_month(2026, 1, -1) == (2025, 12)
    assert shift_month(2026, 12, 1) == (2027, 1)
    assert shift_month(2026, 11, -23) == (2024, 12)


@pytest.mark.parametrize(
    "value",
    [
        "2026-13",
        "2026-00",
        "october",
        "last_0_months",
        "last_25_months",
        "",
        42,
        None,
        {"from": "2026-10-20", "to": "2026-10-05"},
        {"from": "2026-10-20"},
        {"from": "20/10/2026", "to": "2026-10-21"},
        {"from": "2020-01-01", "to": "2026-01-01"},
    ],
)
def test_invalid_periods(value: Any) -> None:
    with pytest.raises(PeriodError):
        parse_period(value, TODAY)


def test_as_dict() -> None:
    assert parse_period("2026-10", TODAY).as_dict() == {
        "from": "2026-10-01",
        "to": "2026-10-31",
        "label": "2026-10",
    }

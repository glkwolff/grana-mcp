from decimal import Decimal

import pytest

from grana_mcp.money import format_brl, to_cents


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        ("19.99", 1999),  # 19.99 * 100 as float is 1998.9999999999998
        ("0.29", 29),
        ("-1234.56", -123456),
        ("2431.5285", 243153),  # Pluggy can send >2 decimals
        ("0.005", 1),  # half-up
        ("5000", 500000),
        ("0", 0),
    ],
)
def test_to_cents(value: str, cents: int) -> None:
    assert to_cents(Decimal(value)) == cents


def test_to_cents_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_cents(19.99)  # type: ignore[arg-type]


def test_format_brl() -> None:
    assert format_brl(123456) == "R$ 1.234,56"
    assert format_brl(-5) == "-R$ 0,05"
    assert format_brl(100000000) == "R$ 1.000.000,00"

"""Money helpers. Money is always an ``int`` number of cents; never ``float``."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def to_cents(value: Decimal) -> int:
    """Convert a decimal amount in currency units to integer cents (half-up rounding).

    Pluggy can return more than two decimal places (e.g. ``2431.5285``), so we round.
    """
    if not isinstance(value, Decimal):  # guard against floats sneaking in
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_brl(cents: int) -> str:
    """Format cents as ``R$ 1.234,56`` (negative values get a leading minus)."""
    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(cents), 100)
    inteiro = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {inteiro},{centavos:02d}"

"""CPF/CNPJ input validation. Documents are only ever kept in memory long enough to
be hashed (``store.db.hash_doc``); nothing here logs or returns them in clear."""

from __future__ import annotations


def _cpf_check_digits(base: str) -> str:
    out = base
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(out[:size], range(size + 1, 1, -1), strict=True))
        rest = total % 11
        out += "0" if rest < 2 else str(11 - rest)
    return out[9:]


def _cnpj_check_digits(base: str) -> str:
    out = base
    for weights in (
        (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
        (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    ):
        total = sum(int(d) * w for d, w in zip(out, weights, strict=False))
        rest = total % 11
        out += "0" if rest < 2 else str(11 - rest)
    return out[12:]


def valid_cpf(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or len(set(digits)) == 1:
        return False
    return _cpf_check_digits(digits[:9]) == digits[9:]


def valid_cnpj(digits: str) -> bool:
    if len(digits) != 14 or not digits.isdigit() or len(set(digits)) == 1:
        return False
    return _cnpj_check_digits(digits[:12]) == digits[12:]


def parse_document(value: str) -> str | None:
    """Digits of a valid CPF or CNPJ (punctuation and spaces allowed), else None."""
    allowed = set("0123456789.-/ ")
    text = value.strip()
    if not text or any(ch not in allowed for ch in text):
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if valid_cpf(digits) or valid_cnpj(digits):
        return digits
    return None

"""pipeline/normalize.py and privacy/documents.py. Documents are computed at
runtime from synthetic bases; no CPF/CNPJ-looking literal is written here."""

from __future__ import annotations

import pytest

from grana_mcp.pipeline.normalize import (
    distinctive_tokens,
    fold,
    is_refund_description,
    refund_key,
    similar_descriptions,
)
from grana_mcp.privacy.documents import parse_document, valid_cnpj, valid_cpf


def _dv(digits: str, weights: list[int]) -> str:
    rest = sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11
    return "0" if rest < 2 else str(11 - rest)


def make_cpf(base9: str) -> str:
    d1 = _dv(base9, list(range(10, 1, -1)))
    return base9 + d1 + _dv(base9 + d1, list(range(11, 1, -1)))


def make_cnpj(base12: str) -> str:
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = _dv(base12, w1)
    return base12 + d1 + _dv(base12 + d1, [6, *w1])


SYNTH_CPF = make_cpf("1029384" + "75")
SYNTH_CNPJ = make_cnpj("1122334" + "4000181"[:5])


def test_valid_documents() -> None:
    assert valid_cpf(SYNTH_CPF)
    assert valid_cnpj(SYNTH_CNPJ)
    wrong = SYNTH_CPF[:-1] + str((int(SYNTH_CPF[-1]) + 1) % 10)
    assert not valid_cpf(wrong)
    assert not valid_cpf("1" * 11)  # repeated digits
    assert not valid_cpf(SYNTH_CPF[:10])
    assert not valid_cnpj("2" * 14)
    assert not valid_cnpj(SYNTH_CNPJ[:-1] + str((int(SYNTH_CNPJ[-1]) + 1) % 10))


def test_parse_document_accepts_punctuation() -> None:
    c = SYNTH_CPF
    formatted = f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}"
    assert parse_document(f"  {formatted} ") == c
    n = SYNTH_CNPJ
    assert parse_document(f"{n[:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:]}") == n


@pytest.mark.parametrize("value", ["", "   ", "abc", "***.123.456-**", "12345", "1" * 11])
def test_parse_document_rejects(value: str) -> None:
    assert parse_document(value) is None


def test_fold_and_refund_key() -> None:
    assert fold("Estorno - Transferência enviada pelo Pix|FULANO 123") == (
        "estorno transferencia enviada pelo pix fulano"
    )
    assert fold(None) == ""
    assert refund_key("Estorno de LOJA X") == "loja x"
    assert refund_key("ESTORNO") == ""
    assert is_refund_description("  Estorno - Compra")
    assert not is_refund_description("Estornos diversos")
    assert not is_refund_description(None)
    assert distinctive_tokens("pix enviado ana a") == {"ana"}


@pytest.mark.parametrize(
    ("refund", "debit", "expected"),
    [
        ("LOJA SINTETICA", "LOJA SINTETICA", True),
        ("Estorno - Compra no débito via NuPay", "Compra no débito via NuPay - LOJA Y", True),
        ("Estorno - Pix|ANA SILVA", "Pix|BIA SILVA", False),
        ("Estorno - Pix|ANA", "Pix|ANABELA", False),  # whole words only
        ("UBER TRIP", "UBER *TRIP HELP 0099", True),
        ("ESTORNO PIX", "PIX ENVIADO", False),  # no distinctive words
        ("", "LOJA", False),
        ("ESTORNO", "LOJA", False),
    ],
)
def test_similar_descriptions(refund: str, debit: str, expected: bool) -> None:
    assert similar_descriptions(refund, debit) is expected

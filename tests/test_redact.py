"""Masking and sanitizing. All names and documents are synthetic (zeros)."""

from __future__ import annotations

import pytest

from grana_mcp.privacy.redact import (
    MAX_TEXT,
    PERSON_LABEL,
    display_text,
    looks_like_person_transfer,
    sanitize_text,
)

HOUSEHOLD = {"hash-da-casa"}


def test_sanitize_strips_control_and_format_chars() -> None:
    assert sanitize_text("LOJA\x00\x1b[31m SINTETICA​‮") == "LOJA[31m SINTETICA"
    assert sanitize_text("A\nB\tC\r\nD") == "A B C D"
    assert sanitize_text("  muitos    espacos  ") == "muitos espacos"


def test_sanitize_truncates() -> None:
    out = sanitize_text("x" * 200)
    assert out is not None
    assert len(out) == MAX_TEXT
    assert out.endswith("…")
    assert sanitize_text("y" * MAX_TEXT) == "y" * MAX_TEXT
    assert sanitize_text("abcdef", limit=4) == "abc…"


def test_sanitize_none() -> None:
    assert sanitize_text(None) is None


@pytest.mark.parametrize(
    ("description", "kwargs", "expected"),
    [
        # Counterpart document known
        ("QUALQUER COISA", {"counterpart_doc_type": "CPF", "counterpart_doc_hash": "h1"}, True),
        ("PIX ENVIADO", {"counterpart_doc_type": "CNPJ", "counterpart_doc_hash": "h2"}, False),
        (
            "PIX ENVIADO PESSOA DA CASA",
            {"counterpart_doc_type": "CPF", "counterpart_doc_hash": "hash-da-casa"},
            False,
        ),
        # Patterns in the description
        ("00.000.000 Pessoa Sintetica", {}, True),
        ("Transferencia enviada - Fulano - ***.000.000-** - BANCO", {}, True),
        ("Pix - •••.000.000-•• - Fulano", {}, True),
        ("PIX ENVIADO 00.000.000/0001-00 EMPRESA", {}, False),
        ("Transferencia enviada pelo Pix - Fulano de Tal", {}, True),
        ("PIX ENVIADO PADARIA SINTETICA LTDA", {}, False),
        ("Pix para iFood", {}, False),
        ("NAO E PIX", {"operation_type": "PIX"}, True),
        ("MERCADO SINTETICO", {}, False),
        ("MERCADO SINTETICO", {"operation_type": "CARTAO"}, False),
        (None, {}, False),
    ],
)
def test_looks_like_person_transfer(
    description: str | None, kwargs: dict[str, str], expected: bool
) -> None:
    assert looks_like_person_transfer(description, household_hashes=HOUSEHOLD, **kwargs) is expected


def test_display_text_masks_or_sanitizes() -> None:
    assert display_text("00.000.000 Pessoa", description="00.000.000 Pessoa") == PERSON_LABEL
    assert (
        display_text("MERCADO\x07 SINTETICO", description="MERCADO SINTETICO")
        == "MERCADO SINTETICO"
    )
    assert display_text(None, description="MERCADO") is None
    # merchant of a masked row is masked too
    assert (
        display_text("Nome Qualquer", description="X", counterpart_doc_type="CPF") == PERSON_LABEL
    )

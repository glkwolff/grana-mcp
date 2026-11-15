"""Masking and sanitizing of third-party text before it reaches the LLM.

Transaction descriptions and merchant names are written by third parties and
may contain prompt-injection attempts or the names of private persons
(HANDOFF §12). Everything that leaves the MCP server goes through
``sanitize_text``, and per-row text goes through ``display_text`` first, which
replaces transfers to natural persons outside the household with a fixed label.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

MAX_TEXT = 80
PERSON_LABEL = "PIX → pessoa física"
ELLIPSIS = "…"

# CPF, full or partially masked: 000.000.000-00, ***.000.000-**, •••.000.000-••
_CPF_RE = re.compile(r"[\d*•xX]{3}\.\d{3}\.\d{3}-[\d*•xX]{2}")
# A CNPJ root followed by a name at the start: "00.000.000 Nome Sobrenome".
# Nubank shows MEI / personal Pix like this; the name is a natural person.
_ROOT_NAME_RE = re.compile(r"^\s*\d{2}\.\d{3}\.\d{3}\s+[^\W\d_]", re.UNICODE)
# Full CNPJ (has the slash): a company, not a person.
_CNPJ_RE = re.compile(r"\d{2}\.?\d{3}\.?\d{3}/\d{4}-?\d{2}")
_TRANSFER_RE = re.compile(
    r"\b(pix|ted|doc|transfer[eê]ncia|transf)\b",
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r"\b(ltda|s\.?/?a|eireli|me|epp|mei|cia|com[eé]rcio|servi[cç]os|"
    r"pagamentos|bank|banco|ifood|uber|99|mercado ?pago|picpay|nubank|ita[uú])\b",
    re.IGNORECASE,
)
_TRANSFER_OPS = frozenset({"PIX", "TED", "DOC", "TRANSFERENCIA_MESMA_INSTITUICAO"})
_WS_RE = re.compile(r"\s+")


def sanitize_text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Drop control/format characters, collapse whitespace and truncate to ``limit``.

    Format characters (category ``Cf``: zero-width, bidi overrides) are removed
    too, since they can hide text from a human reviewer.
    """
    if value is None:
        return None
    cleaned = "".join(
        " " if ch in "\t\n\r\v\f" else ch
        for ch in value
        if ch in "\t\n\r\v\f" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs")
    )
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + ELLIPSIS
    return cleaned


def looks_like_person_transfer(
    description: str | None,
    *,
    counterpart_doc_hash: str | None = None,
    counterpart_doc_type: str | None = None,
    operation_type: str | None = None,
    household_hashes: Collection[str] = (),
) -> bool:
    """True when the row is a transfer to/from a natural person outside the household.

    Privacy first: when in doubt about a transfer with no company marker, mask.
    """
    if counterpart_doc_hash is not None and counterpart_doc_hash in household_hashes:
        return False
    if counterpart_doc_type == "CPF":
        return True
    if counterpart_doc_type == "CNPJ":
        return False
    text = description or ""
    if _CNPJ_RE.search(text):
        return False
    if _CPF_RE.search(text) or _ROOT_NAME_RE.search(text):
        return True
    is_transfer = (operation_type or "").upper() in _TRANSFER_OPS or bool(_TRANSFER_RE.search(text))
    return is_transfer and not _COMPANY_RE.search(text)


def display_text(
    text: str | None,
    *,
    description: str | None,
    counterpart_doc_hash: str | None = None,
    counterpart_doc_type: str | None = None,
    operation_type: str | None = None,
    household_hashes: Collection[str] = (),
) -> str | None:
    """``text`` ready for the LLM: masked if it is a person transfer, then sanitized."""
    if looks_like_person_transfer(
        description,
        counterpart_doc_hash=counterpart_doc_hash,
        counterpart_doc_type=counterpart_doc_type,
        operation_type=operation_type,
        household_hashes=household_hashes,
    ):
        return PERSON_LABEL
    return sanitize_text(text)

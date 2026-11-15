"""Text normalization used by the cleaning pipeline (pure functions).

Descriptions are third-party text; these helpers only compare them, they never
send them anywhere.
"""

from __future__ import annotations

import re
import unicodedata

_NON_LETTER_RE = re.compile(r"[^a-z]+")
_REFUND_PREFIX_RE = re.compile(r"^estorno(\s+(de|do|da|dos|das))?\s+")
_REFUND_START_RE = re.compile(r"^\s*estorno\b", re.IGNORECASE)

# Words that say what kind of operation it was, not who/where. Two refunds of
# "Transferência enviada pelo Pix|<name>" must match on the name, not on these.
GENERIC_WORDS = frozenset(
    {
        "a", "ao", "com", "compra", "credito", "da", "das", "de", "debito", "do", "dos",
        "doc", "e", "em", "enviada", "enviado", "no", "na", "nupay", "o", "para", "pelo",
        "pela", "pix", "recebida", "recebido", "ted", "transf", "transferencia", "via",
    }
)  # fmt: skip


def fold(text: str | None) -> str:
    """Casefold, strip accents, keep only letters (digits/punctuation become spaces)."""
    ascii_text = (
        unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    )
    return " ".join(_NON_LETTER_RE.sub(" ", ascii_text).split())


def is_refund_description(text: str | None) -> bool:
    """Descriptions such as "Estorno - Transferência enviada pelo Pix|…"."""
    return bool(_REFUND_START_RE.match(text or ""))


def refund_key(text: str | None) -> str:
    """Folded description without a leading "estorno" (and "de/do/da")."""
    folded = fold(text)
    if folded == "estorno":
        return ""
    return _REFUND_PREFIX_RE.sub("", folded)


def distinctive_tokens(key: str) -> set[str]:
    return {t for t in key.split() if len(t) >= 2 and t not in GENERIC_WORDS}


def similar_descriptions(a: str | None, b: str | None) -> bool:
    """Whether a refund (``a``) and an earlier debit (``b``) describe the same thing.

    Equal or contained (whole words) normalized text matches. Otherwise the
    distinctive words (names, merchants) need a Jaccard similarity of at least
    0.5, so "Pix|Ana Silva" does not match "Pix|Bia Silva".
    """
    ka, kb = refund_key(a), refund_key(b)
    if not ka or not kb:
        return False
    ta, tb = distinctive_tokens(ka), distinctive_tokens(kb)
    # Whole-word containment ("compra via nupay" in "compra via nupay loja x"),
    # unless the shorter text is a single generic word such as "pix".
    short, short_tokens = (ka, ta) if len(ka) <= len(kb) else (kb, tb)
    contained = f" {ka} " in f" {kb} " or f" {kb} " in f" {ka} "
    if contained and (short_tokens or len(short.split()) >= 2):
        return True
    if not ta or not tb:
        return False
    return len(ta & tb) * 2 >= len(ta | tb)

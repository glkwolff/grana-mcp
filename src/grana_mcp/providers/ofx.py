"""Alternative source: statements exported by the bank (OFX or CSV). No network.

Supported formats:

* **OFX** 1.x (SGML, the usual Brazilian export, often ``CHARSET:1252``) and 2.x
  (XML). Bank statements (``STMTRS``) and credit card statements (``CCSTMTRS``).
  OFX amounts are from the holder's point of view (negative = money out / card
  charge), which is already the internal convention.
* **CSV** ``nubank-cartao`` (``date,title,amount``; positive = purchase),
  ``nubank-conta`` (``Data,Valor,Identificador,Descrição``; negative = out) and
  ``generic`` (``date,description,amount`` with ISO dates; negative = out, or use
  ``invert=True``).

Ids are deterministic hashes, so importing the same file twice never duplicates
rows. Do not import a file for an account that is also synced via Pluggy: the
ids differ and every transaction would be counted twice.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from grana_mcp.money import to_cents
from grana_mcp.providers.base import Account, AccountKind, Transaction

CsvFormat = Literal["nubank-cartao", "nubank-conta", "generic"]
CSV_FORMATS: tuple[CsvFormat, ...] = ("nubank-cartao", "nubank-conta", "generic")
MAX_FILE_BYTES = 20 * 1024 * 1024


class ImportFormatError(ValueError):
    """The file is not in the expected format (message is safe to show)."""


@dataclass(frozen=True, slots=True)
class Statement:
    account: Account
    transactions: list[Transaction]


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def file_account(profile_id: str, label: str, kind: AccountKind, number: str | None) -> Account:
    key = _hash(profile_id, kind, label.casefold(), number or "")
    return Account(
        id=f"file:{key}",
        item_id=f"file:{profile_id}",
        kind=kind,
        name=label,
        number_masked=f"****{number[-4:]}" if number else None,
        currency="BRL",
    )


def _tx(account: Account, seq_key: str, posted: date, cents: int, desc: str) -> Transaction:
    return Transaction(
        id=f"file:{_hash(account.id, seq_key)}",
        account_id=account.id,
        posted_date=posted,
        amount_cents=cents,
        status="POSTED",
        description_raw=" ".join(desc.split())[:200],
        merchant=None,
        provider_category=None,
        operation_type=None,
        installment_number=None,
        installment_total=None,
        counterpart_doc=None,
    )


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ImportFormatError("arquivo grande demais (máx. 20 MB)")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("codificação de texto não reconhecida")


# --- OFX ---

_BLOCK_RE = re.compile(
    r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", re.S | re.I
)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else None


def _ofx_date(value: str | None) -> date:
    if not value or len(value) < 8 or not value[:8].isdigit():
        raise ImportFormatError("data OFX inválida")
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _decimal(value: str | None) -> Decimal:
    if value is None:
        raise ImportFormatError("valor ausente")
    text = value.strip().replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ImportFormatError("valor inválido") from None


def parse_ofx(text: str, profile_id: str, label: str | None = None) -> Statement:
    if "<OFX>" not in text.upper():
        raise ImportFormatError("não parece um arquivo OFX")
    is_card = "<CCSTMTRS>" in text.upper()
    kind: AccountKind = "credit_card" if is_card else "checking"
    acct_id = _tag(text, "ACCTID")
    bank = _tag(text, "ORG") or _tag(text, "BANKID") or "Banco"
    account = file_account(profile_id, label or f"{bank} (OFX)", kind, acct_id)
    txs: list[Transaction] = []
    seen: dict[str, int] = {}
    for block in _BLOCK_RE.findall(text):
        posted = _ofx_date(_tag(block, "DTPOSTED"))
        cents = to_cents(_decimal(_tag(block, "TRNAMT")))
        desc = " ".join(x for x in (_tag(block, "NAME"), _tag(block, "MEMO")) if x) or "?"
        fitid = _tag(block, "FITID")
        if fitid:
            key = f"fitid:{fitid}"
        else:  # no FITID: date+amount+description, numbered for identical rows
            base = f"{posted}:{cents}:{desc}"
            seen[base] = seen.get(base, 0) + 1
            key = f"{base}:{seen[base]}"
        txs.append(_tx(account, key, posted, cents, desc))
    if not txs:
        raise ImportFormatError("nenhuma transação (STMTTRN) encontrada")
    return Statement(account, txs)


# --- CSV ---


def _br_date(value: str) -> date:
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        d, m, y = value.split("/")
        return date(int(y), int(m), int(d))
    except ValueError:
        raise ImportFormatError(f"data inválida: {value[:12]!r}") from None


def parse_csv(
    text: str,
    profile_id: str,
    fmt: CsvFormat,
    label: str | None = None,
    *,
    invert: bool = False,
) -> Statement:
    reader = csv.DictReader(io.StringIO(text))
    kind: AccountKind
    fields = {(f or "").strip().casefold() for f in reader.fieldnames or []}
    if fmt == "nubank-cartao":
        need, kind, sign = {"date", "title", "amount"}, "credit_card", -1
        default_label = "Nubank cartão (CSV)"
    elif fmt == "nubank-conta":
        need, kind, sign = {"data", "valor", "descrição"}, "checking", 1
        default_label = "Nubank conta (CSV)"
    elif fmt == "generic":
        need, kind, sign = {"date", "description", "amount"}, "checking", 1
        default_label = "Conta (CSV)"
    else:
        raise ImportFormatError(f"formato CSV desconhecido: {fmt}")
    if not need <= fields:
        raise ImportFormatError(
            f"colunas esperadas para {fmt}: {', '.join(sorted(need))}; "
            f"encontradas: {', '.join(sorted(fields))[:200]}"
        )
    if invert:
        sign = -sign
    account = file_account(profile_id, label or default_label, kind, None)
    txs: list[Transaction] = []
    seen: dict[str, int] = {}
    for raw_row in reader:
        row = {(k or "").strip().casefold(): (v or "").strip() for k, v in raw_row.items()}
        if fmt == "nubank-cartao":
            posted, amount, desc, ident = _br_date(row["date"]), row["amount"], row["title"], ""
        elif fmt == "nubank-conta":
            posted, amount, desc = _br_date(row["data"]), row["valor"], row["descrição"]
            ident = row.get("identificador", "")
        else:
            posted, amount, desc, ident = (
                _br_date(row["date"]),
                row["amount"],
                row["description"],
                "",
            )
        cents = sign * to_cents(_decimal(amount))
        if ident:
            key = f"id:{ident}"
        else:
            base = f"{posted}:{cents}:{desc}"
            seen[base] = seen.get(base, 0) + 1
            key = f"{base}:{seen[base]}"
        txs.append(_tx(account, key, posted, cents, desc or "?"))
    if not txs:
        raise ImportFormatError("o CSV não tem linhas")
    return Statement(account, txs)

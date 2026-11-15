"""Block personal financial data from being committed (pre-commit hook and CI).

Fails when any given file:
* is a database (*.db, *.sqlite*, *.duckdb) or a bank export (*.ofx, *.csv, *.qfx)
  outside tests/fixtures/synthetic/;
* contains a formatted CPF (000.000.000-00 style) with VALID check digits.
  Synthetic fixtures use invalid ones (e.g. 000.000.000-00), which pass.

Usage: python scripts/check_sensitive.py FILE...   (no args: all git-tracked files)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".duckdb", ".ofx", ".qfx", ".csv"}
ALLOWED_DIR = "tests/fixtures/synthetic/"
CPF_RE = re.compile(rb"(?<!\d)(\d{3})\.(\d{3})\.(\d{3})-(\d{2})(?!\d)")


def valid_cpf(digits: str) -> bool:
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(digits[:size], range(size + 1, 1, -1), strict=True))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def problems(path: str) -> list[str]:
    found = []
    p = Path(path)
    if p.suffix.lower() in BLOCKED_SUFFIXES and not path.startswith(ALLOWED_DIR):
        found.append(f"{path}: arquivo de banco/extrato fora de {ALLOWED_DIR}")
    if p.is_file() and p.stat().st_size < 5_000_000:
        data = p.read_bytes()
        for m in CPF_RE.finditer(data):
            digits = b"".join(m.groups()).decode()
            if valid_cpf(digits):
                line = data[: m.start()].count(b"\n") + 1
                found.append(f"{path}:{line}: parece um CPF válido")
    return found


def main(argv: list[str]) -> int:
    files = (
        argv
        or subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.split()
    )
    errors = [msg for f in files for msg in problems(f)]
    for msg in errors:
        print(msg, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

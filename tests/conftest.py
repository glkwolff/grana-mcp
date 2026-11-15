from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest

# Safety net: even if a test reverts the per-test GRANA_MCP_HOME (e.g. with
# monkeypatch.undo()), paths fall back to a throwaway directory, never to the
# user's real config/data directories.
os.environ["GRANA_MCP_HOME"] = tempfile.mkdtemp(prefix="grana-mcp-tests-")


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring so tests never touch the real system keyring."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    monkeypatch.setenv("GRANA_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GRANA_PLUGGY_CLIENT_ID", raising=False)
    monkeypatch.delenv("GRANA_PLUGGY_CLIENT_SECRET", raising=False)
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)

"""Access tokens for the chat page. Only SHA-256 hashes are stored (file 0600)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path

from grana_mcp import config

_USER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def tokens_path() -> Path:
    return config.config_dir() / "web_tokens.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> dict[str, str]:
    path = tokens_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def _save(data: dict[str, str]) -> None:
    path = tokens_path()
    config.ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def add_user(user: str) -> str:
    """Create or rotate ``user``'s token; returns the plain token (shown once)."""
    user = user.strip().lower()
    if not _USER_RE.match(user):
        raise ValueError("usuário: letras minúsculas, dígitos, '-' ou '_' (máx. 32)")
    token = secrets.token_urlsafe(32)
    data = _load()
    data[user] = _digest(token)
    _save(data)
    return token


def remove_user(user: str) -> bool:
    data = _load()
    if data.pop(user.strip().lower(), None) is None:
        return False
    _save(data)
    return True


def users() -> list[str]:
    return sorted(_load())


def verify(token: str | None) -> str | None:
    """User name for a valid token, else None (constant-time comparison)."""
    if not token:
        return None
    digest = _digest(token)
    found = None
    for user, stored in _load().items():
        if hmac.compare_digest(stored, digest):
            found = user
    return found

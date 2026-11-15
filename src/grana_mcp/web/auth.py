"""Access tokens for the chat page. Only SHA-256 hashes are stored (file 0600)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
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


# --- Tailscale identity (no token needed inside the tailnet) ---

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_WHOIS_TTL_S = 300
_whois_cache: dict[str, tuple[float, str | None]] = {}


def tailscale_path() -> Path:
    return config.config_dir() / "web_tailscale.json"


def _load_ts() -> dict[str, str]:
    path = tailscale_path()
    if not path.exists():
        return {}
    return {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def _save_ts(data: dict[str, str]) -> None:
    path = tailscale_path()
    config.ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def tailscale_add(login: str, user: str) -> None:
    """Map a Tailscale login (e.g. ``ana@github``) to a chat user."""
    login, user = login.strip().lower(), user.strip().lower()
    if "@" not in login or len(login) > 128:
        raise ValueError("login do Tailscale no formato nome@provedor (ex.: ana@github)")
    if not _USER_RE.match(user):
        raise ValueError("usuário: letras minúsculas, dígitos, '-' ou '_' (máx. 32)")
    data = _load_ts()
    data[login] = user
    _save_ts(data)
    _whois_cache.clear()


def tailscale_remove(login: str) -> bool:
    data = _load_ts()
    if data.pop(login.strip().lower(), None) is None:
        return False
    _save_ts(data)
    _whois_cache.clear()
    return True


def tailscale_users() -> dict[str, str]:
    return dict(sorted(_load_ts().items()))


async def whois_login(ip: str, tailscale_bin: str = "tailscale") -> str | None:
    """Tailscale login name of the peer at ``ip`` (via the local tailscaled), or None."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr not in TAILSCALE_NET:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            tailscale_bin, "whois", "--json", ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    try:
        login = json.loads(out)["UserProfile"]["LoginName"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(login).lower() if login else None


async def tailscale_user(ip: str, tailscale_bin: str = "tailscale") -> str | None:
    """Chat user for a request coming from the tailnet, using a short cache."""
    mapping = _load_ts()
    if not mapping:
        return None
    now = time.monotonic()
    cached = _whois_cache.get(ip)
    if cached and now - cached[0] < _WHOIS_TTL_S:
        login = cached[1]
    else:
        login = await whois_login(ip, tailscale_bin)
        _whois_cache[ip] = (now, login)
    return mapping.get(login) if login else None


# --- behind `tailscale serve` (HTTPS) ---
#
# tailscaled terminates TLS and proxies to the container, so every request comes
# from the proxy's address and `whois` would name the PC's owner. It strips any
# Tailscale-User-* headers sent by the client and sets them for the real peer, so
# they are trusted, but ONLY on connections from the configured proxy address.

PROXY_ENV = "GRANA_WEB_TRUSTED_PROXY"  # comma-separated IPs (as the container sees them)
LOGIN_HEADER = "tailscale-user-login"


def trusted_proxies() -> set[str]:
    raw = os.environ.get(PROXY_ENV, "")
    out = set()
    for part in raw.split(","):
        try:
            out.add(str(ipaddress.ip_address(part.strip())))
        except ValueError:
            continue
    return out


def forwarded_peer(header: str | None) -> str | None:
    """Client IP appended by the proxy (last X-Forwarded-For entry), if valid."""
    if not header:
        return None
    try:
        return str(ipaddress.ip_address(header.split(",")[-1].strip()))
    except ValueError:
        return None


def serve_user(ip: str, login: str | None) -> str | None:
    """Chat user named by tailscale serve's header; None if not from the proxy."""
    if not login or ip not in trusted_proxies():
        return None
    return _load_ts().get(login.strip().lower())

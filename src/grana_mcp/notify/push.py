"""Web Push sender (RFC 8030 + 8291 encryption + 8292 VAPID), host side only.

The VAPID private key lives in the system keyring (``GRANA_VAPID_PRIVATE_KEY`` as
fallback), like the Pluggy credentials; only the public key goes to a file in the
config directory, where the web container reads it to subscribe browsers.

Payloads are encrypted for each browser (aes128gcm): the push service (Apple,
Google, Mozilla) only sees the endpoint, the size and the time.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
import keyring
import keyring.errors
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from grana_mcp import config

KEYRING_USER = "vapid"
ENV_PRIVATE_KEY = "GRANA_VAPID_PRIVATE_KEY"
# VAPID "sub": a contact URL for the push service (py_vapid wants a bare https host).
SUBJECT = "https://github.com"
# Push services of Safari/iOS, Chrome/Android and Firefox. Anything else is refused,
# so a forged subscription cannot make the host POST to an arbitrary URL.
ALLOWED_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com")
ALLOWED_SUFFIXES = (".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com")
MAX_ENDPOINT = 1024


class PushError(Exception):
    """Push not configured or invalid input (message is safe to show)."""


@dataclass(frozen=True, slots=True)
class Subscription:
    endpoint: str
    p256dh: str
    auth: str


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key_path() -> Path:
    return config.config_dir() / "push_public_key"


def public_key() -> str | None:
    """Base64url uncompressed P-256 point (the browser's ``applicationServerKey``)."""
    try:
        text = public_key_path().read_text().strip()
    except OSError:
        return None
    return text or None


def _public_b64(vapid: Vapid02) -> str:
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64u(raw)


def setup(*, force: bool = False) -> str:
    """Create the VAPID key pair (private -> keyring, public -> config dir)."""
    if not force and public_key() and _load_private_pem():
        return public_key() or ""
    vapid = Vapid02()
    vapid.generate_keys()
    pem = vapid.private_pem().decode()
    keyring.set_password(config.KEYRING_SERVICE, KEYRING_USER, pem)
    pub = _public_b64(vapid)
    config.ensure_private_dir(config.config_dir())
    path = public_key_path()
    path.write_text(pub + "\n")
    path.chmod(0o600)
    return pub


def _load_private_pem() -> str | None:
    try:
        pem = keyring.get_password(config.KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        pem = None
    return pem or os.environ.get(ENV_PRIVATE_KEY) or None


def load_vapid() -> Vapid02:
    pem = _load_private_pem()
    if not pem:
        raise PushError("Notificações não configuradas: rode 'grana-mcp notify setup'.")
    vapid = Vapid02.from_pem(pem.encode())
    pub = public_key()
    if pub and pub != _public_b64(vapid):
        raise PushError(
            "A chave pública não confere com a do keyring: rode 'notify setup --force'."
        )
    return vapid


def valid_endpoint(url: str) -> bool:
    if not isinstance(url, str) or len(url) > MAX_ENDPOINT:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username:
        return False
    return host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)


def valid_key(text: str, size: int) -> bool:
    try:
        return isinstance(text, str) and len(text) < 200 and len(b64u_decode(text)) == size
    except (ValueError, TypeError):
        return False


def encrypt(payload: bytes, sub: Subscription) -> bytes:
    """aes128gcm body for one browser (fresh sender key and salt each time)."""
    sender = ec.generate_private_key(ec.SECP256R1())
    body: bytes = http_ece.encrypt(
        payload,
        salt=os.urandom(16),
        private_key=sender,
        dh=b64u_decode(sub.p256dh),
        auth_secret=b64u_decode(sub.auth),
        version="aes128gcm",
    )
    return body


def send(
    client: httpx.Client,
    vapid: Vapid02,
    sub: Subscription,
    message: dict[str, Any],
    *,
    ttl: int = 24 * 3600,
    urgency: str = "normal",
) -> int:
    """POST one notification; returns the HTTP status (404/410 = subscription gone)."""
    if not valid_endpoint(sub.endpoint):
        return 410
    parts = urlsplit(sub.endpoint)
    claims = {
        "sub": SUBJECT,
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 3600,
    }
    headers = {str(k): str(v) for k, v in vapid.sign(claims).items()}
    headers.update(
        {
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Urgency": urgency,
        }
    )
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(), sub)
    response = client.post(sub.endpoint, content=body, headers=headers, timeout=15)
    return response.status_code

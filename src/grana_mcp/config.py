"""Paths, profiles and credentials.

* Non-secret config (profiles, display names, Pluggy item ids) lives in
  ``config.json`` under ``platformdirs.user_config_dir("grana-mcp")``.
  Item ids are not secrets.
* Secrets (Pluggy client id/secret) live in the system keyring, service
  ``grana-mcp``, key = profile id. Fallback: ``GRANA_PLUGGY_CLIENT_ID`` /
  ``GRANA_PLUGGY_CLIENT_SECRET`` env vars. Never written to a file or a log.
* ``GRANA_MCP_HOME`` (optional) overrides both directories (``<home>/config``
  and ``<home>/data``); useful for tests and for keeping everything in one place.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import keyring
import keyring.errors
import platformdirs

APP_NAME = "grana-mcp"
KEYRING_SERVICE = "grana-mcp"
ENV_HOME = "GRANA_MCP_HOME"
ENV_CLIENT_ID = "GRANA_PLUGGY_CLIENT_ID"
ENV_CLIENT_SECRET = "GRANA_PLUGGY_CLIENT_SECRET"  # noqa: S105 - env var name, not a secret


def config_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    return Path(home) / "config" if home else platformdirs.user_config_path(APP_NAME)


def data_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    return Path(home) / "data" if home else platformdirs.user_data_path(APP_NAME)


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "grana.db"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@dataclass
class Profile:
    id: str
    display_name: str
    item_ids: list[str] = field(default_factory=list)


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "perfil"


def load_profiles() -> list[Profile]:
    path = config_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Profile(
            id=pid, display_name=p.get("display_name", pid), item_ids=list(p.get("item_ids", []))
        )
        for pid, p in data.get("profiles", {}).items()
    ]


def save_profiles(profiles: list[Profile]) -> None:
    ensure_private_dir(config_dir())
    data = {
        "profiles": {
            p.id: {"display_name": p.display_name, "item_ids": p.item_ids} for p in profiles
        }
    }
    path = config_path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def upsert_profile(profile: Profile) -> None:
    profiles = [p for p in load_profiles() if p.id != profile.id]
    profiles.append(profile)
    save_profiles(profiles)


def get_profile(profile_id: str) -> Profile | None:
    return next((p for p in load_profiles() if p.id == profile_id), None)


# --- credentials ---


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # never print the secret
        return "Credentials(<redacted>)"

    __str__ = __repr__


class CredentialsNotFoundError(Exception):
    pass


def store_credentials(profile_id: str, creds: Credentials) -> None:
    """Save to the system keyring. Raises ``keyring.errors.KeyringError`` if unavailable."""
    payload = json.dumps({"client_id": creds.client_id, "client_secret": creds.client_secret})
    keyring.set_password(KEYRING_SERVICE, profile_id, payload)


def load_credentials(profile_id: str) -> Credentials:
    """Keyring first, then ``GRANA_PLUGGY_CLIENT_ID`` / ``_SECRET`` env vars."""
    try:
        payload = keyring.get_password(KEYRING_SERVICE, profile_id)
    except keyring.errors.KeyringError:
        payload = None
    if payload:
        data = json.loads(payload)
        return Credentials(client_id=data["client_id"], client_secret=data["client_secret"])
    cid, secret = os.environ.get(ENV_CLIENT_ID), os.environ.get(ENV_CLIENT_SECRET)
    if cid and secret:
        return Credentials(client_id=cid, client_secret=secret)
    raise CredentialsNotFoundError(
        f"no Pluggy credentials for profile '{profile_id}' in the keyring "
        f"nor in {ENV_CLIENT_ID}/{ENV_CLIENT_SECRET}"
    )

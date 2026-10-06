"""Configuration: paths, base URL and the API key (loaded from .env, never printed)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"

# Confirmed in docs.panta.market and the official playground's .env.example
DEFAULT_BASE_URL = "https://live-api.panta.market/api/v1"


TRUTHY = {"1", "true", "yes", "on"}


def is_public_mode(env=None) -> bool:
    """PANTA_RADAR_PUBLIC_MODE=1|true|yes|on enables the read-only public deployment (default off)."""
    env = os.environ if env is None else env
    return str(env.get("PANTA_RADAR_PUBLIC_MODE", "")).strip().lower() in TRUTHY


def default_db_path(key: str | None) -> Path:
    """pk_test_ keys only see a sandbox fixture -> separate DB so it never mixes with live data."""
    env = os.getenv("PANTA_RADAR_DB")
    if env:
        return Path(env)
    return DATA_DIR / ("radar_sandbox.db" if (key or "").startswith("pk_test_") else "radar.db")


def mask_key(key: str | None) -> str:
    """pk_test_abcd...wxyz — safe to show. Never returns the full secret."""
    if not key:
        return "<missing>"
    if len(key) <= 16:
        return "<set, too short to mask safely>"
    return f"{key[:12]}...{key[-4:]}"


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    db_path: Path = DATA_DIR / "radar.db"
    timeout_s: float = 20.0
    public_mode: bool = False  # read-only deployment: never calls the Panta API, never holds a key

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)

    def __repr__(self) -> str:  # never leak the key through repr()/logging
        return (f"Settings(api_key={self.masked_key!r}, base_url={self.base_url!r}, "
                f"db_path={str(self.db_path)!r}, public_mode={self.public_mode})")


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    base = (os.getenv("PANTA_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if is_public_mode():
        # Public mode ignores any key that may be present: the public app consumes the
        # GitHub-collected `data` branch only; PANTA_API_KEY lives in GitHub Actions secrets.
        return Settings(api_key=None, base_url=base, db_path=default_db_path(None), public_mode=True)
    key = (os.getenv("PANTA_API_KEY") or "").strip().strip('"').strip("'") or None
    return Settings(api_key=key, base_url=base, db_path=default_db_path(key))

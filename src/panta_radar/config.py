"""Configuration: paths, base URL and the API key (loaded from .env, never printed)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.getenv("PANTA_RADAR_DB") or DATA_DIR / "radar.db")

# Confirmed in docs.panta.market and the official playground's .env.example
DEFAULT_BASE_URL = "https://live-api.panta.market/api/v1"


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
    timeout_s: float = 20.0

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)

    def __repr__(self) -> str:  # never leak the key through repr()/logging
        return f"Settings(api_key={self.masked_key!r}, base_url={self.base_url!r})"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    key = (os.getenv("PANTA_API_KEY") or "").strip().strip('"').strip("'") or None
    base = (os.getenv("PANTA_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return Settings(api_key=key, base_url=base)

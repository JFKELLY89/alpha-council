"""
Alpha Council v2.3 - application settings.

Loads environment variables and YAML configuration. Two hard rules:
  1. Nothing here ever prints or reprs a secret.
  2. Live trading cannot be configured. assert_paper_only() is called at
     process start and raises if anything looks like a live account.

Place at: alpha_council/settings.py
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
DATA_DIR = REPO_ROOT / "data"

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE = "https://api.alpaca.markets"
MARKET_DATA_BASE = "https://data.alpaca.markets"

# Substrings that indicate an unedited template value in SEC_USER_AGENT.
# EDGAR throttles or blocks automated access without a real contact address.
SEC_UA_PLACEHOLDERS = (
    "example.com",
    "your@",
    "youremail",
    "operator@example",
    "your.email",
    "changeme",
)


class PaperOnlyViolation(RuntimeError):
    """Raised when configuration would permit live trading. Never caught."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- application ----
    app_env: str = "development"
    timezone: str = "America/New_York"
    database_path: Path = DATA_DIR / "alpha_council.db"
    config_version: str = "v1"
    log_level: str = "INFO"

    # ---- alpaca ----
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_secret_key: SecretStr = SecretStr("")
    alpaca_paper_trade: bool = True
    alpaca_data_feed: str = "iex"
    alpaca_option_feed: str = "indicative"

    # ---- ai providers ----
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")

    # ---- osint ----
    sec_user_agent: str = "AlphaCouncil/0.1 operator@example.com"

    # ------------------------------------------------------------------
    # validators
    # ------------------------------------------------------------------

    @field_validator("alpaca_data_feed")
    @classmethod
    def _feed_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"iex", "sip", "delayed_sip", "boats", "overnight"}:
            raise ValueError(f"unsupported stock feed: {v}")
        return v

    @field_validator("alpaca_option_feed")
    @classmethod
    def _option_feed_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"indicative", "opra"}:
            raise ValueError(f"unsupported option feed: {v}")
        return v

    @field_validator("sec_user_agent")
    @classmethod
    def _sec_ua_ok(cls, v: str) -> str:
        placeholder = any(p in v.lower() for p in SEC_UA_PLACEHOLDERS)
        if "@" not in v or placeholder:
            raise ValueError(
                "SEC_USER_AGENT must contain a real contact email address. "
                "EDGAR rejects or throttles automated access without one. "
                f"Got: {v!r}"
            )
        return v

    @field_validator("database_path")
    @classmethod
    def _abs_db_path(cls, v: Path) -> Path:
        """Resolve relative paths against the repo root.

        A relative path works from the repo root and breaks the moment the
        scheduler, a test, or a script runs from anywhere else.
        """
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()

    # ------------------------------------------------------------------
    # derived
    # ------------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def trading_base_url(self) -> str:
        if not self.alpaca_paper_trade:
            raise PaperOnlyViolation("alpaca_paper_trade is false")
        return PAPER_TRADING_BASE

    @property
    def data_base_url(self) -> str:
        return MARKET_DATA_BASE

    @property
    def alpaca_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self.alpaca_secret_key.get_secret_value(),
            "Accept-Encoding": "gzip, deflate",
        }

    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key.get_secret_value()
                    and self.alpaca_secret_key.get_secret_value())

    def has_openai(self) -> bool:
        return bool(self.openai_api_key.get_secret_value())

    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    # ------------------------------------------------------------------
    # safety
    # ------------------------------------------------------------------

    def assert_paper_only(self) -> None:
        """Spec Section 2, invariant 2. Call at every process entry point."""
        if not self.alpaca_paper_trade:
            raise PaperOnlyViolation(
                "ALPACA_PAPER_TRADE must be true. Alpha Council refuses to start."
            )
        for name in ("ALPACA_BASE_URL", "APCA_API_BASE_URL"):
            val = os.getenv(name, "")
            if val and "paper-api" not in val:
                raise PaperOnlyViolation(
                    f"{name}={val} points away from the paper endpoint."
                )
        if not self.has_alpaca_credentials():
            raise PaperOnlyViolation("Alpaca credentials are missing.")

    def safe_summary(self) -> dict[str, Any]:
        """Loggable configuration. Contains no secret material by construction."""
        return {
            "app_env": self.app_env,
            "timezone": self.timezone,
            "database_path": str(self.database_path),
            "config_version": self.config_version,
            "alpaca_paper_trade": self.alpaca_paper_trade,
            "alpaca_data_feed": self.alpaca_data_feed,
            "alpaca_option_feed": self.alpaca_option_feed,
            "alpaca_credentials": "present" if self.has_alpaca_credentials() else "MISSING",
            "openai_key": "present" if self.has_openai() else "missing",
            "anthropic_key": "present" if self.has_anthropic() else "missing",
            "sec_user_agent": self.sec_user_agent,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"Settings({self.safe_summary()})"

    __str__ = __repr__


# ----------------------------------------------------------------------
# YAML configuration
# ----------------------------------------------------------------------

def load_yaml(name: str, required: bool = False) -> dict[str, Any]:
    """Load config/<name>.yaml. Returns {} when optional and absent."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        if required:
            raise FileNotFoundError(f"required config missing: {path}")
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_prompt(name: str) -> str:
    """Load config/prompts/<name>.txt."""
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"prompt missing: {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    import json

    s = get_settings()
    ensure_directories()
    print(json.dumps(s.safe_summary(), indent=2))
    try:
        s.assert_paper_only()
        print("\npaper-only check: PASS")
    except PaperOnlyViolation as exc:
        print(f"\npaper-only check: FAIL - {exc}")
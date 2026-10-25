"""Settings, validated once at startup.

Credentials are `SecretStr` so they print as `**********` in a traceback or a
log line. A misconfigured run fails here, before any model call is paid for.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from leadflow.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    hubspot_service_key: SecretStr
    anthropic_api_key: SecretStr
    # Required only for organisation-level keys that are not scoped to a workspace.
    anthropic_workspace_id: str | None = None

    hubspot_base_url: str = "https://api.hubapi.com"
    model: str = "claude-opus-5-5"

    # Bounds the agent loop. A clean lead needs about six steps (two searches,
    # three creates, a final answer); twelve leaves room for a corrected tool
    # input or two without letting a confused run spend indefinitely.
    max_agent_steps: int = Field(default=12, ge=1, le=50)
    max_lead_chars: int = Field(default=8000, ge=100, le=100_000)

    ledger_path: Path = Path(".leadflow/ledger.sqlite3")


def load_settings() -> Settings:
    """Load settings, turning pydantic's error into one that names what is missing."""
    try:
        return Settings()  # values come from the environment and .env
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        raise ConfigError(f"invalid or missing settings: {missing}. See .env.example.") from exc

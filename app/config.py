"""Centralised settings, loaded from environment / .env file.

Everything that differs between local dev and Railway production lives here so
that no code path hardcodes a secret or an environment assumption.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_board_credentials() -> None:
    """Load per-board logins from secrets/board_credentials.env into os.environ.

    Kept separate from .env so the (many) BOARD_* lines don't clutter the main
    config and can be regenerated from the CSV by the seed script. Gitignored.
    """
    path = Path("secrets/board_credentials.env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_board_credentials()


_SQLITE_DEFAULT = "sqlite:///./data/marble.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_base_url: str = "http://localhost:8000"
    database_url: str = _SQLITE_DEFAULT

    @field_validator("database_url", mode="before")
    @classmethod
    def _coerce_blank_db_url(cls, v: str | None) -> str:
        """A blank/whitespace DATABASE_URL (e.g. an unresolved Railway reference)
        would crash engine creation. Fall back to local SQLite instead so the app
        still boots; a warning is logged at engine setup."""
        if v is None or not str(v).strip():
            return _SQLITE_DEFAULT
        return str(v).strip()

    # Ashby
    ashby_api_key: str = ""
    ashby_webhook_secret: str = ""
    ashby_job_board_base: str = "https://jobs.ashbyhq.com/marble"

    # Slack
    # Bot token (xoxb-…) enables threaded status updates via chat.postMessage.
    # The webhook URL is a fallback that can post the initial message but cannot thread.
    slack_bot_token: str = ""
    slack_channel: str = "#talent-ops"
    slack_webhook_url: str = ""

    # Email
    notify_email_to: str = "nadia@marble.studio"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "hiring@marble.studio"

    # Marble constants
    marble_contact_email: str = "hiring@marble.studio"
    marble_contact_name: str = "Nadia Chamana"
    marble_contact_phone: str = "+33749945048"

    @property
    def normalized_database_url(self) -> str:
        """Railway/Heroku hand out `postgres://`, which SQLAlchemy 2 rejects.

        Normalise it to the `postgresql://` scheme psycopg2 expects.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def board_credentials(self, credentials_ref: str) -> tuple[str | None, str | None]:
        """Resolve a board's login from env vars by its credentials_ref.

        credentials_ref "ku-leuven" -> BOARD_KU_LEUVEN_USERNAME / _PASSWORD.
        Passwords are NEVER stored in the DB; they only live in env / Railway secrets.
        """
        key = credentials_ref.upper().replace("-", "_").replace(" ", "_")
        username = os.environ.get(f"BOARD_{key}_USERNAME")
        password = os.environ.get(f"BOARD_{key}_PASSWORD")
        return username, password


@lru_cache
def get_settings() -> Settings:
    return Settings()

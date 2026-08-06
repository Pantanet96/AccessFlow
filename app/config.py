"""Application configuration, loaded from environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Historic default secrets to reject in production (Fix #1).
_WEAK_SECRETS = {"change-me", "change-me-to-a-long-random-string", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    app_secret_key: str = "change-me"
    allow_insecure_secret: bool = False  # dev/test only; bypasses the weak-key guard
    database_path: str = "/data/app.db"
    tz: str = "Europe/Rome"
    default_locale: str = "it"
    public_base_url: str = "http://localhost:8000"

    # SuperAdmin seed
    superadmin_username: str = "admin"
    superadmin_password: str = "change-me"

    # Plex
    plex_token: str = ""
    plex_server_name: str = ""
    plex_client_id: str = ""  # optional; derived from secret if empty
    plex_revoke_on_delete: bool = False
    # Optional: connect straight to this Plex baseurl, skipping plex.tv discovery
    # (eliminates the ~8s LAN-probe timeout). Use the plex.direct dashed-host form
    # so the wildcard TLS cert validates, e.g.
    # https://92-62-83-61.<hash>.plex.direct:9002
    plex_direct_url: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""  # for deep links (without @)

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    smtp_from_name: str = ""  # display name in the From header, e.g. "Plex Manager"
    smtp_tls: bool = True

    # Scheduler / background workers
    notify_hour: int = 9
    enable_scheduler: bool = True
    enable_bot: bool = True
    backup_keep: int = 14

    # Reminder / dunning schedule (comma-separated days; parsed in runtime_config).
    # Admin-editable in Settings; these are only the fallback defaults.
    reminder_days_before: str = "7,3,1"   # pre-expiry buckets (days remaining, >=1)
    reminder_days_after: str = "0,3"      # post-expiry dunning (overdue days, >=0)
    # Manager weekly-digest lookahead window (days). Admin-editable in Settings.
    digest_lookahead_days: int = 14

    def secret_is_weak(self) -> bool:
        """True if APP_SECRET_KEY is a known default or too short (Fix #1)."""
        return self.app_secret_key in _WEAK_SECRETS or len(self.app_secret_key) < 32

    def cookies_secure(self) -> bool:
        """Set the Secure cookie flag only on HTTPS deploys (Fix #4)."""
        return self.public_base_url.lower().startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""settings_store.rotate_secret re-encrypts every value to the new key."""
from app.models import AppSetting
from app.services import settings_store


def test_rotate_secret_reencrypts(db_session):
    s = db_session
    settings_store.set_value(s, "plex_token", "secret-token-123")

    new = "rotated-secret-key-0123456789"  # >= 16 chars
    try:
        count = settings_store.rotate_secret(s, new)
        assert count >= 1

        # No corruption window: the running process switches to the new key
        # immediately, so the rotated value is readable right away.
        assert settings_store.get_value(s, "plex_token") == "secret-token-123"
        # And on disk it's encrypted under the new key.
        row = s.get(AppSetting, "plex_token")
        plain = settings_store._fernet_from(new).decrypt(row.value.encode()).decode()
        assert plain == "secret-token-123"
    finally:
        # Global override must not leak into other tests (they use the base key).
        settings_store._active_secret_override = None

"""settings_store.rotate_secret re-encrypts every value to the new key."""
from app.models import AppSetting
from app.services import settings_store


def test_rotate_secret_reencrypts(db_session):
    s = db_session
    settings_store.set_value(s, "plex_token", "secret-token-123")

    new = "rotated-secret-key-0123456789-abcdef"  # >= 32 chars
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


def test_rotate_route_rejects_key_startup_would_refuse(client, db_session, login_as):
    # 16..31 chars used to rotate fine, then crash-looped the container once
    # APP_SECRET_KEY was set to it (startup requires 32).
    from app.models import AppUser, Role

    boss = AppUser(role=Role.superadmin, real_name="Boss")
    db_session.add(boss)
    db_session.commit()
    login_as(client, boss.id)
    try:
        resp = client.post("/settings/rotate-key", data={"new_secret": "x" * 20})
        assert "at least 32 characters" in resp.text
        assert settings_store._active_secret_override is None
    finally:
        settings_store._active_secret_override = None

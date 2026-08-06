# Security notes

Details behind the summary in the [README](../README.md#security).

## Secrets

- `APP_SECRET_KEY` is mandatory: random, ≥32 characters. The app refuses to start with a weak/default key.
  Generate one with:
  ```bash
  python -c "import secrets;print(secrets.token_urlsafe(48))"
  ```
- Only in dev/test you can bypass the check with `ALLOW_INSECURE_SECRET=true`. Never in production.
- Application secrets (Plex token, SMTP password, Telegram token) are encrypted at rest with Fernet, using `APP_SECRET_KEY` as the key material.

## SuperAdmin account

If `SUPERADMIN_PASSWORD` is left empty or default, a random password is generated and logged **once** on first boot (as a warning in the container logs). Log in with it and change it right away from `/profile`.

## Reverse proxy / IP spoofing

`FORWARDED_ALLOW_IPS` tells uvicorn which source IPs are allowed to set `X-Forwarded-*` headers. Set it to your reverse proxy's IP or subnet (e.g. `172.18.0.0/16`).

Do **not** leave it as `*` on an app exposed directly to the internet: a spoofable `X-Forwarded-For` header would let an attacker bypass the per-IP login lockout.

The image also honors `X-Forwarded-Proto` (`--proxy-headers`), so behind an HTTPS proxy generated URLs come out as `https`, and the session cookie gets the `Secure` flag automatically when `PUBLIC_BASE_URL` starts with `https://`.

## Container image

- The image never contains `.env`, `data/`, or `*.db` files — they're excluded via `.dockerignore`, so a leaked/pushed image can't leak your secrets or database.
- Data persists only in the `appdata` volume mounted at `/data` (database + backups).
- The container runs as an unprivileged user (uid `10001`): make sure the `/data` volume is writable by that uid.

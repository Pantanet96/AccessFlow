# Security notes

Details behind the summary in the [README](../README.md#security).

## Secrets

- `APP_SECRET_KEY` is mandatory: random, ≥32 characters. The app refuses to start with a weak/default key.
  Generate one with:
  ```bash
  python -c "import secrets;print(secrets.token_urlsafe(48))"
  ```
- Only in dev/test you can bypass the check with `ALLOW_INSECURE_SECRET=true`. Never in production: with an https `PUBLIC_BASE_URL` the app logs a warning at every start. To move off a weak key without losing the stored secrets, rotate it from Settings > System, set `APP_SECRET_KEY` to the new key and remove `ALLOW_INSECURE_SECRET` (a new key set by hand makes the encrypted settings unreadable).
- Application secrets (Plex token, SMTP password, Telegram token) are encrypted at rest with Fernet, using `APP_SECRET_KEY` as the key material.

## SuperAdmin account

If `SUPERADMIN_PASSWORD` is left empty or default, a random password is generated on first boot and written to `INITIAL_SUPERADMIN_PASSWORD.txt` in the data folder (only its path is logged). Log in with it, change it from `/profile` and delete the file: the app shows a warning until you do.

A new password needs at least 12 characters, must not contain the username or be a common password, and is capped at 72 bytes (bcrypt ignores anything longer). The password from the environment is accepted at first boot; the app checks it at each login and warns while it does not meet these rules.

### Login lockout

Five failed logins from one IP within 15 minutes lock that IP out, each time for longer: 5, 10, 15, 30, 60, 120, then 240 minutes. After a day without failures it starts again from 5. Failures on one username only slow that username down without blocking it, so nobody can lock the superadmin out on purpose. Counters live in memory and reset on restart.

### Two-step verification

Turn it on from `/profile` with any TOTP authenticator app. Once on, the code is asked after the password and after "Sign in with Plex" too: a Plex sign-in link can be phished (whoever started it gets the session when you approve it on plex.tv), the code stops that. You get 10 one-time recovery codes: store them away from the phone. Secret and codes are encrypted like the other secrets (see above).

Lost the phone and the codes? Set `SUPERADMIN_MFA_RESET=true`, restart, log in with the password, then remove the variable and turn it on again.

### Sessions

A sign-in lasts 14 days; activity does not extend it. Signing out, or changing the password, ends that account's sessions on every device.

## CSRF

Every form submission must come from the app's own pages: the browser's `Sec-Fetch-Site` header (https) or `Origin` header (plain http) is checked, and a request carrying neither is refused. Every current browser sends them; a browser extension that strips `Origin` will see "CSRF check failed".

## Reverse proxy / IP spoofing

`FORWARDED_ALLOW_IPS` tells uvicorn which source IPs are allowed to set `X-Forwarded-*` headers. Set it to your reverse proxy's IP or subnet (e.g. `172.18.0.0/16`).

Do **not** leave it as `*` on an app exposed directly to the internet: a spoofable `X-Forwarded-For` header would let an attacker bypass the per-IP login lockout.

The image also honors `X-Forwarded-Proto` (`--proxy-headers`), so behind an HTTPS proxy generated URLs come out as `https`. When `PUBLIC_BASE_URL` starts with `https://`, the session and sign-in cookies get the `Secure` flag and the `__Host-` prefix automatically: no other subdomain can set or overwrite them. Switching `PUBLIC_BASE_URL` between http and https signs everyone out once.

## Integrations

Plex and Overseerr URLs are set by the superadmin and trusted as such: private addresses are allowed, because Overseerr usually runs on one (`http://overseerr:5055`).

## Container image

- The image never contains `.env`, `data/`, or `*.db` files — they're excluded via `.dockerignore`, so a leaked/pushed image can't leak your secrets or database.
- Data persists only in the `appdata` volume mounted at `/data` (database + backups).
- The container runs as an unprivileged user (uid `10001`): make sure the `/data` volume is writable by that uid.

# AccessFlow guide

Full reference. For the short version see the [README](../README.md).

## Contents

- [How it works](#how-it-works)
- [Deploy](#deploy)
- [Security](#security)
- [Configuration](#configuration)
- [Background workers](#background-workers)
- [i18n](#i18n)
- [Local development](#local-development)

## How it works

Manages the lifecycle of a Plex server's users: who has access, what plan they're on,
when it expires, who collects the payment. The idea is to stop having to track "who
owes me and when" by hand.

### Roles

- **SuperAdmin** — full access, local login (username/password). Configures the system.
- **Admin** — manages users, plans, reports, settings.
- **Moderator** — manages *only the users assigned to them* (their "clients") and
  collects their payments. Cannot touch free plans or global settings.
- **User** — sees only their own subscription.

Every regular user has a **manager** (an Admin or Moderator): the person who brought
them in and collects their payments. This way multiple people can manage their own
users under the same server, each one seeing only their own.

### Plans

By default only two special plans exist:

- **Family & Friends** — free, never expires.
- **Trial** — timed trial period (max 30 days), not renewable.

**Paid plans** (with custom price and duration) are created by the SuperAdmin as
needed from the Plans page — there are no predefined ones.

### Typical workflows

**1. Adding a new paying user**
   1. The admin invites the person on Plex from the portal (sends the Plex invite and a guide email).
   2. The person accepts and logs in with their own Plex account (PIN flow, no password to manage).
      An invite not accepted within 30 days expires and the Plex share is withdrawn; the
      admin, or the future user's manager, can extend it by 30 days from the home page.
   3. They're assigned a paid plan → the subscription starts and the **initial payment
      is recorded right away** (it goes into the reports as revenue).
   4. It's also possible to pay several months in advance: you set the number of
      periods and the expiration is calculated accordingly.

**2. Renewal (two-step)**
   1. At expiration the manager creates a **renewal** → it stays *pending* until collected.
   2. When the client pays, the manager marks it as paid, indicating the **payment
      method** (e.g. "PayPal", "cash"). Only then is the expiration extended and the
      revenue counted.
   - Multiple periods can be renewed at once.

**3. Expiration reminders (automatic)**
   - A daily job checks who's about to expire and sends reminders via **email** and
     **Telegram** (default: 7/3/1 days before, and 0/3 days after for overdue follow-ups).
   - Reminders are deduplicated (they don't repeat on the same day).
   - The manager receives a "to be collected" notice.

**4. Reports**
   - User count per plan + revenue summary: previous month / collected this
     month / to be collected this month / next month's projection.
   - Every euro in the reports comes from a recorded payment (initial setup or a paid renewal).

**5. Telegram**
   - Users link their own Telegram account to the portal to receive reminders.
   - Admin can send manual broadcasts.

Other automations: audit log of every action, soft-delete with orphan protection,
nightly backup of the SQLite database.

## Deploy

The image is published on Docker Hub (**public** repo): `pantanet96/accessflow`.

```bash
cp .env.example .env   # fill in the secrets, never commit the real .env
docker compose up -d
```

App at `http://localhost:8000`, behind your reverse proxy (NPM / Traefik / Caddy).
Health check: `GET /healthz`.

`docker-compose.yml`:

```yaml
services:
  app:
    image: pantanet96/accessflow:latest
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - appdata:/data
    restart: unless-stopped
volumes:
  appdata:
```

**Upgrading**: `docker compose pull && docker compose up -d`.

> The image honors `X-Forwarded-Proto` (`--proxy-headers`), so behind an HTTPS proxy URLs come out as `https`.

### Unraid

Ships a Community Applications template → [UNRAID.md](UNRAID.md).

## Security

- `APP_SECRET_KEY` is mandatory (random, ≥32 chars) — the app won't start without one.
- SuperAdmin password auto-generates on first boot if left blank (written to a file in the data folder); change it from `/profile`.
- Optional two-step verification (TOTP), asked after both the password and the Plex sign-in, with one-time recovery codes.
- Failed logins lock the IP out for longer each time (5 min up to 4 h); new passwords need 12+ characters.
- Set `FORWARDED_ALLOW_IPS` to your reverse proxy's subnet — never `*` on a directly exposed app.
- Runs as an unprivileged container user; secrets are encrypted at rest.

Full details → [SECURITY.md](SECURITY.md).

## Configuration

Everything via environment / `.env` — see [.env.example](../.env.example).

### Identity shown to Plex

Every request the app makes to Plex carries `AccessFlow` as product, device and
device name, so `plex.tv` → Settings → Authorized Devices and Tautulli's logs
name the app instead of `Linux` + the container hostname. The platform *version*
is left alone and reports the host kernel release (a container shares the host
kernel), so you end up with e.g. `AccessFlow / 6.6.78-Unraid`.

Running more than one instance against the same Plex account? Give each a
distinct device name so you can tell their tokens apart when revoking one:

```yaml
services:
  app:
    environment:
      PLEXAPI_HEADER_DEVICE_NAME: "AccessFlow-staging"
```

Leave `PLEXAPI_HEADER_PRODUCT` as is — Plex and Tautulli group activity by
product, and changing it splits your own history in their dashboards.

## Background workers

The web container also runs, in-process: a daily APScheduler job at the `NOTIFY_HOUR`
hour (expiration scan and reminders, auto-suspension, expired invites, manager digests),
Plex reconciliation and DB backups every few hours, and the Telegram bot in polling mode.
Dates, reminders and report months follow the `TZ` timezone.
They're toggled with `ENABLE_SCHEDULER` / `ENABLE_BOT`.

## i18n

Source strings are in English. Translations live in `app/translations/<locale>/LC_MESSAGES/messages.po`.
After changing templates/strings:

```bash
pybabel extract -F babel.cfg -o messages.pot .
pybabel update -i messages.pot -d app/translations
# edit the .po files, then:
pybabel compile -d app/translations
```

The Docker build compiles the catalogs automatically.


## Local development

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # prod deps + pytest (prod uses requirements.txt)
export DATABASE_PATH=./data/app.db   # avoids the container's /data path
uvicorn app.main:app --reload
pytest
```


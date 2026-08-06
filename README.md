# AccessFlow

Self-hosted web portal to manage users, subscriptions, and invites for a personal Plex server.
FastAPI + Jinja2/HTMX + SQLite, in a single Docker container behind your reverse proxy.
AccessFlow is an independent project, not affiliated with Plex, Inc. — see the disclaimer below for details.

> **Private** repo. This README contains only what's needed to deploy it, run the
> operational scripts, and understand how the application works.

## Independence disclaimer

AccessFlow is an independent project and is not affiliated with, sponsored by, endorsed by, or in any way officially connected to Plex, Inc. or its registered trademarks. The name "Plex" is used in this repository solely for descriptive purposes, to indicate interoperability with Plex's official public APIs.

AccessFlow does not modify, circumvent, or alter the Plex software in any way, does not download or distribute media content, and does not process payments on behalf of third parties: it only tracks access expirations and periods already decided by the server administrator.

Anyone who installs and operates an instance of AccessFlow is solely responsible for their use of it, including compliance with Plex's Terms of Service, copyright law, and any applicable regulations in their jurisdiction. The project's authors assume no liability for misuse of the software.

---

## What it is and how it works (in brief)

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
   1. The admin/moderator invites the person on Plex from the portal (sends the Plex invite).
   2. The person accepts and logs in with their own Plex account (PIN flow, no password to manage).
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

---

## Deploy (production)

The image is published on Docker Hub (**public** repo): `pantanet96/accessflow`.

### With Portainer (recommended)

1. Create a **stack** with this `docker-compose.yml`:

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

2. Load the environment variables (see [.env.example](.env.example)) — **never** commit the real `.env`.
3. Deploy. App at `http://<host>:8000`, behind your reverse proxy (NPM / Traefik / Caddy).
4. **Upgrading to a new version**: re-pull the image (`latest` or a specific tag) and redeploy the stack.

> The image honors `X-Forwarded-Proto` (`--proxy-headers`), so behind an HTTPS proxy URLs come out as `https`.

### With docker compose (also for local dev)

```bash
cp .env.example .env   # fill in the secrets
docker compose up --build
```

App at `http://localhost:8000`. Health check: `GET /healthz`.

### Image / security notes

- The image **never** contains `.env`, `data/`, or `*.db` files (excluded via `.dockerignore`).
- Data persists in the `appdata` volume mounted at `/data` (DB + backups).
- Application secrets are encrypted at rest (Fernet) using `APP_SECRET_KEY`.
- **`APP_SECRET_KEY` is mandatory**: random, ≥32 characters. The app refuses to start with
  a weak/default key (`python -c "import secrets;print(secrets.token_urlsafe(48))"`).
  Only in dev/test can you set `ALLOW_INSECURE_SECRET=true`.
- **SuperAdmin password**: if `SUPERADMIN_PASSWORD` is empty/default, a random one is
  generated and logged **once** on first boot (warning) — log in and change it from `/profile`.
- **`FORWARDED_ALLOW_IPS`**: set it to the reverse proxy's IP/subnet (e.g. `172.18.0.0/16`).
  Don't leave it as `*` on an app exposed directly (a spoofable XFF header bypasses the per-IP lockout).
- The container runs as an unprivileged user (uid `10001`): the `/data` volume must be
  writable by that uid.
- Session cookie gets the `Secure` flag automatically when `PUBLIC_BASE_URL` is `https://`.

---

## Local development

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # prod deps + pytest (prod uses requirements.txt)
export DATABASE_PATH=./data/app.db   # avoids the container's /data path
uvicorn app.main:app --reload
pytest
```

## Configuration

Everything via environment / `.env` — see [.env.example](.env.example).

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

## Background workers

The web container also runs, in-process: a daily APScheduler job (expiration scan
at the `NOTIFY_HOUR` hour, nightly DB backup) and the Telegram bot in polling mode.
They're toggled with `ENABLE_SCHEDULER` / `ENABLE_BOT`.

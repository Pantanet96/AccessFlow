# AccessFlow

**Self-hosted portal to manage users, subscriptions, and invites for a personal Plex server.**

FastAPI + Jinja2/HTMX + SQLite, shipped as a single Docker container that sits behind your reverse proxy.

[![Docker Image](https://img.shields.io/badge/docker-pantanet96%2Faccessflow-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/pantanet96/accessflow)
![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/status-private-lightgrey)

> **Private repo.** This README contains only what's needed to deploy it, run the
> operational scripts, and understand how the application works.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Deploy](#deploy)
- [Security](#security)
- [Local development](#local-development)
- [Configuration](#configuration)
- [i18n](#i18n)
- [Background workers](#background-workers)

## Features

- **Role-based access** — SuperAdmin, Admin, Moderator, User, each scoped to only what they need.
- **Multi-manager support** — every user has a manager who collects their payments; multiple people can run their own client base under the same server.
- **Flexible plans** — free/trial plans out of the box, custom paid plans created on demand.
- **Two-step renewals** — a renewal stays *pending* until the payment is actually collected, then extends the expiration.
- **Automatic reminders** — expiration notices via email and Telegram, deduplicated, configurable schedule.
- **Financial reports** — revenue collected, pending, and projected, straight from recorded payments.
- **Telegram bot** — users link their account for reminders; admins can broadcast.
- **Audit log, soft-delete, nightly backups** — out of the box.
- **i18n** — English source strings, translatable via `.po` catalogs.

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

## Security

- `APP_SECRET_KEY` is mandatory (random, ≥32 chars) — the app won't start without one.
- SuperAdmin password auto-generates on first boot if left blank; change it from `/profile`.
- Set `FORWARDED_ALLOW_IPS` to your reverse proxy's subnet — never `*` on a directly exposed app.
- Runs as an unprivileged container user; secrets are encrypted at rest.

Full details → [docs/SECURITY.md](docs/SECURITY.md).

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

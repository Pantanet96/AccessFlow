# Deploy on Unraid

The image is public on Docker Hub (`pantanet96/accessflow`), no compose file needed on Unraid.

## Option A — add as a template repository (recommended)

1. **Settings → Docker → Template repositories** → add:
   ```
   https://github.com/Pantanet96/AccessFlow
   ```
2. **Apps → search "AccessFlow"** (or **Docker → Add Container** → pick it from the template dropdown).
3. Fill in at minimum:
   - **App Secret Key** — random string ≥32 chars (`python -c "import secrets;print(secrets.token_urlsafe(48))"`).
   - **Plex Token** / **Plex Server Name** — to manage/invite Plex users.
   - **Public Base URL** — your `https://` URL if it sits behind a reverse proxy (NPM, SWAG, Traefik).
4. Leave **Appdata** at `/mnt/user/appdata/accessflow` (holds the SQLite DB, backups, encrypted secrets).
5. Apply. WebUI on port 8000.

## Option B — manual container

**Docker → Add Container**, fill in by hand:

| Field | Value |
|---|---|
| Repository | `pantanet96/accessflow:latest` |
| Port | `8000:8000` |
| Path | `/mnt/user/appdata/accessflow` → `/data` |
| Variable | `APP_SECRET_KEY` = *(random ≥32 chars)* |

Then add the rest of the variables you need from [.env.example](../.env.example).

## Reverse proxy

Behind NPM/SWAG/Traefik on the Unraid docker bridge, set:

- `PUBLIC_BASE_URL=https://accessflow.yourdomain.tld`
- `FORWARDED_ALLOW_IPS=172.17.0.0/16` (Unraid's default bridge subnet — adjust if yours differs)

Without this the app won't mark session cookies `Secure` and won't trust `X-Forwarded-Proto`, so redirects/links may come out `http://` behind an HTTPS proxy.

## Upgrading

**Docker tab → check for updates**, or just re-pull `pantanet96/accessflow:latest` and restart the container. The SQLite DB in `/data` (mapped to appdata) survives updates; nightly backups also land there.

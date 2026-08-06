FROM node:20-slim AS assets

WORKDIR /app

COPY package.json .
RUN npm install
COPY tailwind.config.js .
COPY src ./src
COPY app/templates ./app/templates
RUN npm run build

# Babel lives here and nowhere else. It is a build tool (.po -> .mo) whose
# CLDR locale-data weighs ~33MB; app/i18n.py reads the compiled catalogs with
# stdlib gettext, so only the .mo files cross into the runtime image.
FROM python:3.12-slim AS i18n

WORKDIR /app
RUN pip install --no-cache-dir babel==2.18.0
COPY app/translations ./app/translations
# No "|| true": a broken .po must fail the build, not silently ship an image
# with missing translations.
RUN pybabel compile -d app/translations

FROM python:3.12-slim

LABEL org.opencontainers.image.title="AccessFlow"
LABEL org.opencontainers.image.description="Self-hosted Plex user management tool"
LABEL org.opencontainers.image.source="https://github.com/Pantanet96/AccessFlow"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/app.db

WORKDIR /app

COPY requirements.txt .
# pip removes itself last (~7MB): nothing in the running container installs
# packages, and an image that cannot pip-install is one less way in.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip

COPY . .
COPY --from=assets /app/app/static/app.css /app/app/static/app.css
COPY --from=i18n /app/app/translations ./app/translations

# Run as an unprivileged user. Only /data (the volume) is chowned to it: the app
# code under /app stays root-owned and read-only to appuser, so a code-exec
# foothold can't rewrite templates/.py inside the running container to persist.
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser \
    && mkdir -p /data && chown -R appuser:appuser /data

VOLUME ["/data"]
EXPOSE 8000
USER appuser

# --proxy-headers + trusted forwarders only (Fix #3): set FORWARDED_ALLOW_IPS to
# your reverse proxy's IP/subnet (e.g. 172.18.0.0/16). Defaults to localhost; do
# NOT leave it at "*" on an app exposed directly. Trusting the proxy lets uvicorn
# honor X-Forwarded-Proto so url_for() emits https (no mixed-content blocking).
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]

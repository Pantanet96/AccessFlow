"""FastAPI application factory + app instance."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.auth.deps import get_current_user
from app.config import get_settings
from app.db import get_session
from app.i18n import detect_locale, set_current_locale
from app.models import AppUser
from app.routers import access as access_router
from app.routers import auth as auth_router
from app.routers import broadcast as broadcast_router
from app.routers import invites as invites_router
from app.routers import plans as plans_router
from app.routers import audit as audit_router
from app.routers import profile as profile_router
from app.routers import reports as reports_router
from app.routers import settings as settings_router
from app.routers import subscriptions as subscriptions_router
from app.routers import tutorial as tutorial_router
from app.routers import users as users_router
from app.templating import BASE_DIR, templates


def _allowed_origins(request: Request) -> set[str]:
    """Origins accepted for same-origin state-changing requests: the configured
    public base URL and the request's own scheme+host (proxy headers resolved)."""
    allowed: set[str] = set()
    base = get_settings().public_base_url.rstrip("/")
    if base:
        allowed.add(base)
    host = request.headers.get("host")
    if host:
        allowed.add(f"{request.url.scheme}://{host}")
    return allowed


def warm_plex_cache() -> None:
    """Populate plex_service's connection + sections caches. Must never raise:
    it runs on a startup thread nobody is waiting on, and a cold cache is only a
    slow first request, not a broken app."""
    import logging

    from app.services import plex_service
    from app.services.plex_service import PlexNotConnected

    log = logging.getLogger("pum.startup")
    try:
        plex_service.list_sections()
    except PlexNotConnected:
        # Normal on a fresh install / Plex-less setup. Not worth a warning.
        log.debug("Plex cache warm-up skipped: Plex is not connected")
    except Exception:  # noqa: BLE001 - Plex unreachable, bad token, slow network
        # Plex *is* configured but we could not reach it: worth surfacing, since
        # the same failure will hit the first user request.
        log.warning("Plex cache warm-up failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.secret_is_weak() and not settings.allow_insecure_secret:
        raise RuntimeError(
            "APP_SECRET_KEY è debole o di default. Imposta una chiave casuale di almeno "
            "32 caratteri (es. `python -c \"import secrets;print(secrets.token_urlsafe(48))\"`). "
            "Per sviluppo locale puoi impostare ALLOW_INSECURE_SECRET=true."
        )
    app.state.telegram_bot = None  # exposed to /healthz; stays None if bot is off
    try:
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    from sqlmodel import Session

    from app.db import engine
    from app.migrate import run_migrations
    from app.seed import seed_all

    run_migrations()
    with Session(engine) as session:
        seed_all(session)

    scheduler = None
    if settings.enable_scheduler:
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()

        # Pay Plex's cold connect (plex.tv discovery + LAN probe, ~8-22s — see
        # plex_service._account_and_server) here instead of on whoever opens
        # /users first, which is what made that page slow on the first hit.
        #
        # Daemon thread so it can never delay shutdown, and never fatal: if Plex
        # is off/unreachable/unconfigured the cache stays empty and list_sections()
        # just does its normal thing on the first real request. Racing a request
        # is harmless — both would refresh the same module-level cache with a
        # whole-dict rebind, which concurrent requests can already do today.
        #
        # Gated on enable_scheduler because that is already this app's "background
        # work is allowed here" switch; tests set it false, so no stray threads.
        import threading

        threading.Thread(
            target=warm_plex_cache, name="plex-warmup", daemon=True
        ).start()

    bot = None
    if settings.enable_bot:
        from app.runtime_config import telegram_config

        if telegram_config()["token"]:
            from app.bot.runner import start_bot

            try:
                bot = await start_bot()
                app.state.telegram_bot = bot  # expose to /healthz probe
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Telegram bot failed to start (bad token or network) — "
                    "continuing without it.", exc_info=True,
                )
                bot = None

    try:
        yield
    finally:
        if bot is not None:
            from app.bot.runner import stop_bot

            await stop_bot(bot)
        if scheduler is not None:
            from app.scheduler import shutdown_scheduler

            shutdown_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="AccessFlow", lifespan=lifespan)

    app.mount(
        "/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static"
    )

    # Self-hosted app, all assets served locally -> a tight CSP works. Inline
    # <script>/<style> blocks remain, hence 'unsafe-inline' (no external sources).
    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    @app.middleware("http")
    async def csrf_origin_middleware(request: Request, call_next):
        """Reject cross-site state-changing requests. The app has no CSRF tokens;
        SameSite=Lax alone still permits login CSRF (POST /login needs no cookie)
        and does not distinguish a compromised same-site subdomain. We enforce an
        Origin/Sec-Fetch-Site check on every unsafe method instead — no per-form
        token needed, and it also covers the unauthenticated login form.

        Non-browser clients (no Origin, no Sec-Fetch-Site — e.g. curl, health
        probes, the test client) are allowed: CSRF is a browser-only threat."""
        if request.method not in _SAFE_METHODS:
            sfs = request.headers.get("sec-fetch-site")
            if sfs is not None:
                # Modern browsers: only a same-origin (or user-initiated "none")
                # request is trusted; "same-site" (sibling subdomain) and
                # "cross-site" are rejected.
                if sfs not in ("same-origin", "none"):
                    return PlainTextResponse("CSRF check failed", status_code=403)
            else:
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") not in _allowed_origins(request):
                    return PlainTextResponse("CSRF check failed", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def locale_middleware(request: Request, call_next):
        locale = detect_locale(request)
        set_current_locale(locale)
        response = await call_next(request)
        if request.query_params.get("locale") == locale:
            response.set_cookie(
                "locale", locale, max_age=31536000, httponly=False,
                secure=get_settings().cookies_secure(),
            )
        return response

    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(access_router.router)
    app.include_router(invites_router.router)
    app.include_router(subscriptions_router.router)
    app.include_router(broadcast_router.router)
    app.include_router(reports_router.router)
    app.include_router(plans_router.router)
    app.include_router(settings_router.router)
    app.include_router(profile_router.router)
    app.include_router(audit_router.router)
    app.include_router(tutorial_router.router)

    @app.get("/healthz")
    def healthz(request: Request):
        """Deep liveness/readiness probe.

        CRITICAL checks (database, scheduler-when-enabled) drive the HTTP status:
        200 when all pass/skip, 503 when any fails. Telegram and Plex are
        INFORMATIONAL and never change the status code. Plex is reported from
        persisted/cached state only — no live plex.tv call.
        """
        from fastapi.responses import JSONResponse

        settings = get_settings()
        checks: dict[str, dict] = {}
        critical_ok = True

        # 1. Database (CRITICAL) — SELECT 1
        try:
            from sqlmodel import Session as _S
            from sqlmodel import text

            from app.db import engine

            with _S(engine) as s:
                s.exec(text("SELECT 1")).first()
            checks["database"] = {"status": "pass"}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"status": "fail", "error": str(exc)}
            critical_ok = False

        # 2. APScheduler (CRITICAL only when enabled)
        if not settings.enable_scheduler:
            checks["scheduler"] = {"status": "skipped", "reason": "disabled by config"}
        else:
            try:
                from app import scheduler as sched_module

                sched = sched_module._scheduler
                if sched is None or not sched.running:
                    checks["scheduler"] = {"status": "fail", "reason": "not running"}
                    critical_ok = False
                else:
                    jobs = sched.get_jobs()
                    next_runs = [j.next_run_time for j in jobs if j.next_run_time]
                    if not next_runs:
                        checks["scheduler"] = {
                            "status": "fail", "reason": "no jobs with next_run_time",
                            "jobs": len(jobs),
                        }
                        critical_ok = False
                    else:
                        checks["scheduler"] = {
                            "status": "pass", "jobs": len(jobs),
                            "next_run": min(next_runs).isoformat(),
                        }
            except Exception as exc:  # noqa: BLE001
                checks["scheduler"] = {"status": "fail", "error": str(exc)}
                critical_ok = False

        # 3. Telegram bot polling (INFORMATIONAL — never trips 503)
        if not settings.enable_bot:
            checks["telegram_bot"] = {"status": "skipped", "reason": "disabled by config"}
        else:
            try:
                from app.runtime_config import telegram_config

                if not telegram_config()["token"]:
                    checks["telegram_bot"] = {"status": "skipped", "reason": "no token"}
                else:
                    bot_app = getattr(request.app.state, "telegram_bot", None)
                    polling = bool(
                        bot_app is not None
                        and bot_app.running
                        and bot_app.updater is not None
                        and bot_app.updater.running
                    )
                    checks["telegram_bot"] = {
                        "status": "pass" if polling else "fail", "polling": polling,
                    }
            except Exception as exc:  # noqa: BLE001
                checks["telegram_bot"] = {"status": "fail", "error": str(exc)}

        # 4. Plex token (INFORMATIONAL, cheap/cached — no live call)
        try:
            from app.runtime_config import plex_config

            cfg = plex_config()
            if not cfg["token"]:
                checks["plex"] = {"status": "skipped", "reason": "no token"}
            elif cfg["account_email"]:
                checks["plex"] = {"status": "pass", "cached": True}
            else:
                checks["plex"] = {
                    "status": "unknown", "reason": "token set but no cached validation",
                }
        except Exception as exc:  # noqa: BLE001
            checks["plex"] = {"status": "fail", "error": str(exc)}

        # Anonymous endpoint: expose only pass/fail/skip per check. Error strings
        # (which can include DB paths), job counts, next-run times and whether
        # Plex/Telegram tokens are configured are internal detail — don't leak them.
        public_checks = {name: {"status": c.get("status")} for name, c in checks.items()}
        body = {"status": "ok" if critical_ok else "unhealthy", "checks": public_checks}
        return JSONResponse(body, status_code=200 if critical_ok else 503)

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        current_user: AppUser | None = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        if current_user is None:
            return RedirectResponse("/login", status_code=303)
        ctx = {"current_user": current_user}
        if current_user is not None:
            from app.models import Plan
            from app.services import subscriptions as sub_svc

            sub = sub_svc.get_active_subscription(session, current_user.id)
            plan = session.get(Plan, sub.plan_id) if sub else None
            manager = (
                session.get(AppUser, current_user.manager_id)
                if current_user.manager_id
                else None
            )
            days_left = None
            if sub and sub.expiry_at:
                from app.models import utcnow

                days_left = (sub.expiry_at.date() - utcnow().date()).days
            ctx.update(
                {
                    "sub": sub,
                    "plan": plan,
                    "manager": manager,
                    "days_left": days_left,
                    "pending_renewal": sub_svc.get_pending_renewal(session, sub.id)
                    if sub
                    else None,
                    "renewals": sub_svc.list_renewals(session, sub.id) if sub else [],
                }
            )
        from app import runtime_config

        _ov = runtime_config.overseerr_config()
        ctx["overseerr_url"] = _ov["public_url"] if _ov["enabled"] else ""
        if current_user.role.value != "user":
            from app.services.worklist import build_worklist

            ctx["worklist"] = build_worklist(session, current_user)
        return templates.TemplateResponse(request, "index.html", ctx)

    return app


app = create_app()
